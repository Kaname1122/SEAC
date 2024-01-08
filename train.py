import glob
import logging
import os
import shutil
import time
from collections import deque
from os import path
from pathlib import Path

import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter

import utils
from a2c import A2C, algorithm
from envs import make_vec_envs
from wrappers import RecordEpisodeStatistics, SquashDones
from model import Policy

import wandb

# import rware # noqa
import lbforaging # noqa

logging.basicConfig(
    level=logging.INFO,
    format="(%(process)d) [%(levelname).1s] - (%(asctime)s) - %(name)s >> %(message)s",
    datefmt="%m/%d %H:%M:%S",
)

config = {
    "env_name" : "Foraging-15x15-3p-4f-v2",
    "seed": 219,
    "time_limit" : 25,
    "wrappers" : (
        RecordEpisodeStatistics,
        SquashDones,
    ),
    "dummy_vecenv" : False,

    "num_env_steps" : 5e7,  # 100e6

    "eval_dir" : "./results/video/{id}",
    "loss_dir" : "./results/loss/{id}",
    "save_dir" : "./results/trained_models/{id}",

    "log_interval" : 100,
    "save_interval" : int(1e6),
    "eval_interval" : int(1e6),
    "episodes_per_eval" : 8,
}

run = wandb.init(
    project="lab",
    name="seac_foraging",
    config=config,
)


def _squash_info(info):
    info = [i for i in info if i]
    new_info = {}
    keys = set([k for i in info for k in i.keys()])
    keys.discard("TimeLimit.truncated")
    for key in keys:
        mean = np.mean([np.array(d[key]).sum() for d in info if key in d])
        new_info[key] = mean
    return new_info

def evaluate(
    agents,
    monitor_dir,

    episodes_per_eval=config["episodes_per_eval"],
    env_name=config["env_name"],
    seed=config["seed"],
    wrappers=config["wrappers"],
    dummy_vecenv=config["dummy_vecenv"],
    time_limit=config["time_limit"],
):
    device = algorithm["device"]

    eval_envs = make_vec_envs(
        env_name,
        seed,
        dummy_vecenv,
        episodes_per_eval,
        time_limit,
        wrappers,
        device,
        monitor_dir=monitor_dir,
    )

    n_obs = eval_envs.reset()
    n_recurrent_hidden_states = [
        torch.zeros(
            episodes_per_eval, agent.model.recurrent_hidden_state_size, device=device
        )
        for agent in agents
    ]
    masks = torch.zeros(episodes_per_eval, 1, device=device)

    all_infos = []

    while len(all_infos) < episodes_per_eval:
        with torch.no_grad():
            _, n_action, _, n_recurrent_hidden_states = zip(
                *[
                    agent.model.act(
                        n_obs[agent.agent_id], recurrent_hidden_states, masks
                    )
                    for agent, recurrent_hidden_states in zip(
                        agents, n_recurrent_hidden_states
                    )
                ]
            )

        # Obser reward and next obs
        n_obs, _, done, infos = eval_envs.step(n_action)

        n_masks = torch.tensor(
            [[0.0] if done_ else [1.0] for done_ in done],
            dtype=torch.float32,
            device=device,
        )
        all_infos.extend([i for i in infos if i])

    eval_envs.close()
    info = _squash_info(all_infos)
    logging.info(
        f"Evaluation using {len(all_infos)} episodes: mean reward {info['episode_reward']:.5f}\n"
    )

def main(
    num_env_steps=config["num_env_steps"],
    env_name=config["env_name"],
    seed=config["seed"],
    algorithm=algorithm,
    dummy_vecenv=config["dummy_vecenv"],
    time_limit=config["time_limit"],
    wrappers=config["wrappers"],
    save_dir=config["save_dir"],
    eval_dir=config["eval_dir"],
    loss_dir=config["loss_dir"],
    log_interval=config["log_interval"],
    save_interval=config["save_interval"],
    eval_interval=config["eval_interval"],
):

    if loss_dir:
        loss_dir = path.expanduser(loss_dir.format(id=str(run.id)))
        utils.cleanup_log_dir(loss_dir)
        writer = SummaryWriter(loss_dir)
    else:
        writer = None

    eval_dir = path.expanduser(eval_dir.format(id=str(run.id)))
    save_dir = path.expanduser(save_dir.format(id=str(run.id)))

    utils.cleanup_log_dir(eval_dir)
    utils.cleanup_log_dir(save_dir)

    torch.set_num_threads(1)
    envs = make_vec_envs(
        config["env_name"],
        config["seed"],
        config["dummy_vecenv"],
        algorithm["num_envs"],
        config["time_limit"],
        config["wrappers"],
        algorithm["device"],
    )

    agents = [
        A2C(i, osp, asp)
        for i, (osp, asp) in enumerate(zip(envs.observation_space, envs.action_space))
    ]
    obs = envs.reset()

    # Magic
    for agent in agents:
        wandb.watch(agent.model, log_freq=100)

    for i in range(len(obs)):
        agents[i].storage.obs[0].copy_(obs[i])
        agents[i].storage.to(algorithm["device"])

    start = time.time()
    num_updates = (
        int(config["num_env_steps"]) // algorithm["num_steps"] // algorithm["num_envs"]
    )

    all_infos = deque(maxlen=10)
    environment_steps = 0
    for j in range(1, num_updates + 1):
        environment_steps += algorithm["num_steps"] * algorithm["num_envs"]
        for step in range(algorithm["num_steps"]):
            # Sample actions
            with torch.no_grad():
                n_value, n_action, n_action_log_prob, n_recurrent_hidden_states = zip(
                    *[
                        agent.model.act(
                            agent.storage.obs[step],
                            agent.storage.recurrent_hidden_states[step],
                            agent.storage.masks[step],
                        )
                        for agent in agents
                    ]
                )
            # Obser reward and next obs
            obs, reward, done, infos = envs.step(n_action)
            # envs.envs[0].render()

            # If done then clean the history of observations.
            masks = torch.FloatTensor([[0.0] if done_ else [1.0] for done_ in done])

            bad_masks = torch.FloatTensor(
                [
                    [0.0] if info.get("TimeLimit.truncated", False) else [1.0]
                    for info in infos
                ]
            )
            for i in range(len(agents)):
                agents[i].storage.insert(
                    obs[i],
                    n_recurrent_hidden_states[i],
                    n_action[i],
                    n_action_log_prob[i],
                    n_value[i],
                    reward[:, i].unsqueeze(1),
                    masks,
                    bad_masks,
                )

            for info in infos:
                if info:
                    all_infos.append(info)

        # value_loss, action_loss, dist_entropy = agent.update(rollouts)
        for agent in agents:
            agent.compute_returns()

        for agent in agents:
            loss = agent.update([a.storage for a in agents])
            for k, v in loss.items():
                # for tensorboard
                if writer:
                    writer.add_scalar(f"agent{agent.agent_id}/{k}", v, j)

        for agent in agents:
            agent.storage.after_update()

        if j % config["log_interval"] == 0 and len(all_infos) > 1:
            squashed = _squash_info(all_infos)

            total_num_steps = (
                (j + 1) * algorithm["num_envs"] * algorithm["num_steps"]
            )
            end = time.time()
            logging.info(
                f"Updates {j}, num timesteps {total_num_steps}, FPS {int(total_num_steps / (end - start))}"
            )
            logging.info(
                f"Last {len(all_infos)} training episodes mean reward {squashed['episode_reward'].sum():.3f}"
            )
            squashed["environment_steps"] = environment_steps
            wandb.log(squashed)
            all_infos.clear()

        if config["save_interval"] is not None and (
            j > 0 and j % config["save_interval"] == 0 or j == num_updates
        ):
            cur_save_dir = path.join(save_dir, f"u{j}")
            for agent in agents:
                save_at = path.join(cur_save_dir, f"agent{agent.agent_id}")
                os.makedirs(save_at, exist_ok=True)
                agent.save(save_at)
            archive_name = shutil.make_archive(cur_save_dir, "xztar", save_dir, f"u{j}")
            shutil.rmtree(cur_save_dir)
            run.log_artifact(archive_name)

        if config["eval_interval"] is not None and (
            j > 0 and j % config["eval_interval"] == 0 or j == num_updates
        ):
            evaluate(
                agents, os.path.join(eval_dir, f"u{j}"),
            )
            videos = glob.glob(os.path.join(eval_dir, f"u{j}") + "/*.mp4")
            for i, v in enumerate(videos):
                run.log_artifact(v, f"u{j}.{i}.mp4")
    envs.close()

if __name__ == "__main__":
    main()
