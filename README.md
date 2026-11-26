# 跨窗口就业转介与面谈归属

联调数据采用 [《转介和面谈交换约定》](交换约定.md) 的字段和时间口径。`data/referrals_20261207.json` 是各参与方的合成报文快照，含未核认、互相冲突或处于变化中的来源事实。

后端只使用 Python 3.11 标准库，提供日常转介收件、身份核验/异议/分拆、材料来源追溯、授权校验、跟进窗口判定、联系人转交与企业确认、时间冲突处理，以及求职者聚合查询。

## 检查与启动

```sh
python3 -m unittest discover -s tests -v
python3 app.py                 # 默认 http://127.0.0.1:8000
PORT=8040 python3 app.py       # 也可用 PORT 指定端口
```

默认状态首次启动时从交换快照初始化，并原子写入 `var/employment-state.json`。可用 `EMPLOYMENT_STATE=/path/to/state.json` 修改位置；删除该文件即可重新从快照初始化。

原有的只读报文服务仍然可用：

```sh
python3 -m http.server 8040 --bind 127.0.0.1 --directory data
```

## 一次查询回答三个问题

```sh
curl -sS 'http://127.0.0.1:8000/api/job-seeker/view?registrant=R1&at=13:00' \
  | python3 -m json.tool
```

返回中的关键字段：

- `material_deliveries[*].items[*].delivered`：已经企业收件确认的材料项；未收到回执的投送只显示 `sent_awaiting_receipt`，不会被算作已送达。
- `current_contact` 与 `appointments[*].contact`：当前联系人窗口、待企业确认的转交、原窗口和原席位。
- `action_required.appointments_to_confirm`：仍需本人选择/确认的约见。
- `action_required.overlap_clusters`：相交且尚未由本人决定的有效邀约。
- `provenance.by_station`：每个资料项/转介来自哪个窗口。

快照中的几个有用时点：

| `at` | 业务状态 |
| --- | --- |
| `09:10` | J1 的 A1、A2 都在等收件确认；没有独占跟进窗口，先提交不产生优先权 |
| `09:20` | A1 已收件，ST1 凭“授权 + 收件确认”负责 |
| `12:40` | 求职者已选 ST2；V1 的正式联系人仍为 ST1，但有待企业确认的 ST2 转交 |
| `12:45` | 企业确认联系人变化；席位仍是 `W13`，时间仍是 15:00–15:30 |
| `13:00` | V1/V2 时间相交且本人尚未选择；两边都不能报爽约 |
| `13:10` 后 | C4 选择 V1/W13；V2 保留可查，列入待取消而不是自动删除 |
| `16:00` 后 | I3 分拆 R1/R2；材料来源仍可按 ST1/ST2 追溯，已确认的 V1 联系人变化保留 |

不显式传 `at` 时，样本服务默认按快照最终状态（16:00）查询；正式日常调用可传 ISO 时间或省略 `at` 使用当前时间。

## 身份异议与材料来源

```sh
# 提交疑似重复身份，等待核验
curl -sS -X POST http://127.0.0.1:8000/api/identity-cases \
  -H 'Content-Type: application/json' \
  -H 'X-Station-Id: ST1' \
  -d '{"records":["R1","R2"],"reason":"相同联系方式线索"}'

# 对已合并案例提出异议
curl -sS -X POST http://127.0.0.1:8000/api/identity-cases/I1/objection \
  -H 'Content-Type: application/json' \
  -H 'X-Station-Id: ST2' \
  -d '{"station":"ST2","reason":"本人否认同一身份"}'

# 确认合并错误后分拆，并得到材料来源归属
curl -sS -X POST http://127.0.0.1:8000/api/identity-cases/I1/split \
  -H 'Content-Type: application/json' \
  -H 'X-Station-Id: ST2' \
  -d '{"station":"ST2","at":"16:00","reason":"核验后确认认错"}'

curl -sS 'http://127.0.0.1:8000/api/materials/provenance?identity_case=I3'
```

`hint_id` 和 `checked_contact` 只作为人工核验线索；单个相同不会自动归并。只有 `merge` 核验结论会把记录纳入同一个当前求职过程，后续 `split` 可按时点拆开。

窗口核实材料时必须带 `X-Station-Id`。材料的 `source_station` 取自信任的窗口身份；事后只有来源窗口能修改该核实记录。

## 授权、机会批次和旧回执

授权严格匹配四要素：求职者 `registrant`、企业 `employer`、正式机会 `opportunity`、资料项 `fields`，并检查 `start`/`expires`/`withdrawn`。J1 与 J2 即使企业、岗位名和 `legacy_key`（`A-7`）相同，也是不同 opening；G1/J1 不能用于 J2。

```sh
# 撤回授权：只阻止之后的内容披露，不取消已有约见
curl -sS -X POST http://127.0.0.1:8000/api/grants/G2/withdraw \
  -H 'Content-Type: application/json' -d '{"at":"13:30"}'

# 关闭原机会；之后可重开为新的 opening/opportunity id
curl -sS -X POST http://127.0.0.1:8000/api/opportunities/J1/close \
  -H 'Content-Type: application/json' -d '{"at":"14:00"}'
curl -sS -X POST http://127.0.0.1:8000/api/opportunities/J1/reopen \
  -H 'Content-Type: application/json' -d '{"at":"14:05"}'
```

旧入口只有雇主和 `legacy_key`。不带 `opening` 且编号曾复用时，接口返回 `409 opening_required`，不会猜测最新岗位。回执若引用原转介 A2，则始终按 A2 提交时的 J1/opening=1 核对：

```sh
curl -sS -X POST http://127.0.0.1:8000/api/exchange/legacy-receipt \
  -H 'Content-Type: application/json' \
  -d '{"employer":"E1","legacy_key":"A-7","referral_id":"A2","received_at":"13:05"}'
```

## 跟进权与联系人转交

判定顺序：

1. `owner_choice`：求职者选择的窗口优先；
2. 无选择时，授权匹配且已取得企业收件确认的转介窗口负责；
3. 没有已收件的有效转介时，不产生独占负责人；多个窗口同时等待收件也不能按提交时间抢独占权；
4. 同一机会在身份分拆后属于不同求职过程时，各自保留来源和已确认的联系人关系。

转交已有面谈：

```sh
# 当前窗口发起；必须能找到求职者选择或单独授权。记录返回的转交 id。
TRANSFER_ID=$(curl -sS -X POST http://127.0.0.1:8000/api/appointments/V1/transfer \
  -H 'Content-Type: application/json' -H 'X-Station-Id: ST1' \
  -d '{"target_station":"ST2","at":"12:35","owner_choice_id":"C1"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# 企业确认联系人变化；不会创建新席位
curl -sS -X POST "http://127.0.0.1:8000/api/transfers/${TRANSFER_ID}/acknowledge" \
  -H 'Content-Type: application/json' -H 'X-Employer-Id: E1' \
  -d '{"at":"12:45"}'
```

也可直接投递交换事件：`POST /api/exchange/updates`，支持 `owner_choice`、`acknowledge_contact`、`received`、`choose_slot`、`closed`、`cancelled`。

## 时间冲突、选择和取消

- 相交邀约在本人决定前保持可查，`POST /api/appointments/{id}/no-show` 返回 `409 overlap_decision_pending`。
- `POST /api/appointments/{id}/slot-choice` 记录本人选择；默认不自动删除另一个约见，只把它列入 `appointments_to_cancel`。
- 本人可另行调用 `POST /api/appointments/{id}/cancel`。撤回授权不会代其取消约见，取消约见也不会自动撤回授权。

## 企业隔离

企业接口必须带 `X-Employer-Id`：

```sh
curl -sS -H 'X-Employer-Id: E1' \
  http://127.0.0.1:8000/api/employer/opportunities/J1/referrals
```

该接口只返回 J1 这一 exact opportunity 批次。E2 访问 J1、或 E1 试图通过 J2/其他旧编号探测 J1，均得到 `404` 或仅返回所询问机会自己的空集合；响应不包含身份图谱、其他机会授权或其他窗口材料。

## 主要 HTTP 接口

- `GET /health`
- `GET /api/job-seeker/view?registrant=R1[&at=13:00][&appointment=V1]`
- `GET /api/ownership?opportunity=J1[&registrant=R1][&at=12:40]`
- `POST /api/referrals` / `POST /api/referrals/{id}/receipt`
- `POST /api/exchange/updates` / `POST /api/exchange/legacy-receipt`
- `POST /api/identity-cases` / `.../{id}/decision|objection|split`
- `GET /api/materials/provenance?registrant=R1` 或 `?identity_case=I3`
- `POST /api/materials/verifications`
- `POST /api/grants` / `POST /api/grants/{id}/withdraw`
- `POST /api/opportunities` / `.../{id}/close|reopen`
- `POST /api/appointments` / `.../transfer|contact-acknowledgement|slot-choice|cancel|no-show`
- `POST /api/transfers/{id}/acknowledge`
- `GET /api/employer/opportunities/{id}/referrals`
