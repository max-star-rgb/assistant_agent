# Langfuse 案例目录

本目录只保存 Dataset item 定义，不保存 Dataset、Suite、Profile、Evaluator 或运行结果。

```text
cases/
  legacy/       # eval engineering workflow 建立前的待迁移案例
  engineered/   # 按 capability、独立证据和校准流程设计的新案例
```

规则：

- `legacy/behavior_pre_eval_engineering.cases.json` 是可整体删除的迁移边界；
- `engineered/` 默认一个 capability 一个版本化 case 文件，可包含该 capability 的必要回归变体；
- 新案例不得加入 `legacy/`，也不得直接写入 `datasets/`；
- Dataset composition 在 `../datasets/*.dataset.json` 中按顺序引用 case source；
- 将 legacy 案例完成重设计、校准和真实 Experiment 审计后，在 `engineered/` 创建对应文件，再从
  legacy collection 删除原 item；
- legacy item 全部迁移后，删除 `legacy/` 并从 behavior composition 移除该 source；
- 删除本地 legacy source 不删除 Langfuse 中已有的历史 Dataset、Experiment 或 Score。

`draft -> calibrated -> active -> retired` 是 engineered case 的生命周期，不是目录层级。Fixture 放在
`../fixtures/<capability>/`，Judge 校准样本放在 `../evaluators/calibration/`。
