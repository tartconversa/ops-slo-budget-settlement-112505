# 按用户请求结算错误预算与变更额度

边缘与服务可靠性结算演练。时间为演练起点后的分钟。每条客户端或边缘事实由 source 和 ref 唯一标识，其 revision 数字只在相同来源标识内递增。

## 在本地查看资料

运行环境使用 Python 3.11.2 和标准库。启动资料浏览器无需安装额外包。

```sh
python materials.py
python materials.py objectives
python -m unittest discover -s tests -v
```

资料浏览器也接受下方的其他资料名。JSONL 使用 UTF-8 编码，每行是一个对象。表内的顺序对应来源记录，事件时间与接收时间的区别以资料说明为准。各入口可以在无外网的环境中运行。

资料字段和业务口径见[演练资料格式](materials/README.md)。
