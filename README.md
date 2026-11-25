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

## 预算结算与变更额度台

`slo_budget` 包从同一个入口持续接收边缘、客户端、服务结算记录与变更申请，
在本地完成全部裁决、授权与留痕，不调用任何外部系统。

### 三套必须分开看的数字

报表不会把结算压成一个健康比例。发版负责人需要同时看到：

1. **实时可用余量**：当前坏请求单位 + 缺证预留 + 变更占用（未使用也占余量）后还能批多少；
2. **当前重述的月度结算**：含逾期补证、更正、撤销的最新裁决；补证期满仍无法裁决的
   未知请求按坏请求保守计入，绝不按零失败；
3. **当时授权依据**：每次批准/开工那一刻已有证据的快照，后续补证只改重述，不回改授权。

### 裁决口径（摘要）

* 逻辑请求以 intake id 为准，跨机房重试只是同一请求下的多份边缘尝试；
  边缘已成功的可交付结果不会被另一次独立超时替代。
* 有效客户端终态能证明用户可见结果，与边缘冲突时以客户端为准；
  撤销使同 ref 旧事实失效，失效后回到边缘口径。
* 有客户端收件渠道但终态迟到：补证期内保守预留一个单位；边缘有明确终态可先暂结，
  缺证风险与预留保留到补证期满。无客户端渠道时零占用等待边缘证据。
* 降级、时延按该请求**初次进入账期**的服务承诺判定，条款修订不回溯已接纳请求。
* 共享 edge 对同一逻辑请求最多计一个单位，任一下游未达成即计一次，不重复放大。
* 变更争用时先保护已开工消耗；未开工申请按原到达顺序重审，尚可改动的变更在证据
  更正后回到可决策状态；仍有亏空的 scope 不会因其他撤单获得新额度。

### 本地演练命令

建议用隔离模式运行，资料目录显式传参，避免不可信资料目录中的同名模块被导入：

```sh
python -I rehearsal.py --directory materials report      # 完整文本报表
python -I rehearsal.py --directory materials reconcile   # 当时授权 vs 当前重述的差异解释
python -I rehearsal.py --directory materials journal     # 全量裁决与授权留痕（JSONL）
python -I rehearsal.py --directory materials summary     # 三套数字的机器可读 JSON
python -I rehearsal.py --directory materials snapshot --output state/snapshot.json
```

接入新一月的记录时，把新账期的 `objectives` 与当月 `intake/facts/changes` 放入同一
资料目录从同一入口回放即可；请求按 `minute` 落入对应账期，跨月的逾期补证仍记入
其原属账期的当前重述。运行测试：

```sh
python -m unittest discover -s tests -v
```
