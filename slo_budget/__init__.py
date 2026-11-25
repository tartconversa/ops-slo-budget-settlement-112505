"""预算结算与变更额度台：只依赖标准库。

领域模块划分：

* :mod:`slo_budget.model`      承诺、逻辑请求、证据事实、变更动作的不可变值对象
* :mod:`slo_budget.evidence`   证据裁决：用户可见结果、重试副本、允许的降级、补证期限
* :mod:`slo_budget.ledger`     错误预算台账：实时余量、当前重述、当时授权三套数字
* :mod:`slo_budget.desk`       变更额度台：申请、开工、争用、撤销与回流
* :mod:`slo_budget.engine`     事件溯源编排：按接收时间持续吞入记录并留痕
* :mod:`slo_budget.reports`    报表：把三套数字分开呈现，不以单个健康比例代替决策
* :mod:`slo_budget.cli`        同一个入口：接入新一月资料、回放、重算、对账、报表
"""

__all__ = ["model", "evidence", "ledger", "desk", "engine", "reports", "cli"]
