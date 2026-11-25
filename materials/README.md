# 演练资料格式

边缘与服务可靠性结算演练。时间为演练起点后的分钟。每条客户端或边缘事实由 source 和 ref 唯一标识，其 revision 数字只在相同来源标识内递增。

## `objectives` 服务与共享承诺

scope 是服务名称或共用链路。month_start 等价于账期含端点的第一分钟，holds_until_min 为新变更可回看的补证期限长度。error_units 是该期允许的坏请求单位数，每个逻辑失败贡献一个单位。fallback_ok 和 latency_ms 为服务用户承诺。 holds_until_min 从首次接纳时刻计算。未确定的请求在所涉额度中先预留一个单位，到结算期限仍无法裁决时按坏请求保守结算并标为未知。共享 edge 只根据同一请求的任一涉及服务是否未达成承诺计算一次。

字段为 `scope`、`month_start`、`month_end`、`error_units`、`fallback_ok`、`latency_ms`、`holds_until_min`。

## `intake` 边缘首次承接的逻辑请求

services 列出客户本次会收到的响应涉及的服务。client_receipt 表明有独立客户端回执渠道，false 时按服务承诺选用边缘终态。retries 也进入 facts，逻辑请求仍用此处 id。

字段为 `minute`、`request`、`services`、`client_receipt`。

## `facts` 响应证据与来源更正

at 为接收时间。service 为证据覆盖的用户响应部分。outcome 有 received、failed、unknown、withdrawn。fallback 反映用户收到的版本。source/ref 对下级有更正作用，revision 只在相同来源标识内递增。 client 的有效 received/failed 是本逻辑请求对该服务的用户级终态，边缘终态按尝试区分。多次边缘尝试以客户端首次请求总时长和最终可交付结果为准，边缘已成功的可交付结果不能被独立的单次超时替代。撤销行让同 ref 的旧事实失效。

字段为 `at`、`request`、`service`、`source`、`ref`、`revision`、`outcome`、`latency_ms`、`fallback`。

## `changes` 变更经理提交的申请和动作

period_start 为额度所属承诺期。units 在全部 scopes 上同时占用。kind 为 apply、begin、finish、withdraw。begin 后的额度是已经使用的授权记录。

字段为 `at`、`action`、`kind`、`period_start`、`scopes`、`units`。
