# SEAC
卒論比較用

wandbを使って実験データの整理を行う

実行方法
```Python
python train.py
```

実験環境やハイパーパラメータは`train.py`を直接書き換える

環境  
python 3.7  
gym == 0.21  

gym0.21を入れるに伴って  
setuptools==66  
importlib-metadata<5.0  
をインストールする

gymインストール後
/usr/local/python3.7/lib/python3.7/site-packages/wheel/vendored/packaging/requirements.py
を以下のように編集する
```python
def init(self, requirement_string: str) -> None:
  try:
  
          if requirement_string.find('opencv-python>=3.')>=0:
  
              requirement_string += "0"    # opencv-python>=3.0
  
          parsed = parse_requirement(requirement_string)
```
