# voicekit

把一个人的文风变成**能数出来的判据**，然后拿这些判据去体检稿子、按他的腔起稿。

不是"AI 替你写"。是**起稿机 + 挑刺器**：先给你一个像他的草稿，再逐条告诉你哪里不像。
放不放行永远是作者签字，不是分数。

## 装

```bash
pip install writer-voice     # 发行名是 writer-voice，装完命令叫 voicekit
```

装用 PyPI 包名 `writer-voice`；模块名和命令名都是 `voicekit`。以后选名字时这点说清楚省得找错。

本地开发：

```bash
py -3.10 -m pip install -e .
```

只依赖标准库 + PyYAML。`lint` / `report` 不花钱（不调模型）。`build` 只在 `style_distill` 环节要模型；
`draft` 和 `judge` 要模型。

## 五条命令

```bash
# 1. 体检一篇稿子（0 元）
voicekit lint 我的稿子.md --pack voices/panchen-voice

# 2. 一页 HTML 报告：结论 3 行 + 先改这三条 + 全部 42 条（0 元）
voicekit report 我的稿子.md --pack voices/panchen-voice

# 3. 从一个语料目录提炼作者 profile（一篇一个 .md）
voicekit build --corpus 语料/ --out voices/某人-voice --slug 某人 --author 某人

# 4. 按作者的腔起稿（要 key）
set DEEPSEEK_API_KEY=...        # PowerShell: $env:DEEPSEEK_API_KEY="..."
voicekit draft --material 材料.md --dry-run   # 先看提示词，0 元
voicekit draft --material 材料.md --out 初稿.md

# 5. 判同人：拿 10 篇作者样例判这篇像不像同一个人（要 key，只作参考）
voicekit judge 我的稿子.md --pack voices/panchen-voice --dry-run   # 0 元：只看提示词规模
voicekit judge 我的稿子.md --pack voices/panchen-voice
```

`judge` 退出码：`0` 像 / `1` 不像 / `2` 判不了或环境错（缺基线、基线过期、包与样例集不匹配）。
它要读作者原文，所以包里必须有 `corpus`（或样例集里记的 corpus 目录）。判定会打印 tokens 与 `cost_usd`；
**它说像不等于作者肯签字**，放行仍由人拍板。

`lint` 退出码看的是「像不像同一个人」（`0` 像 / `1` 不像 / `2` 读不了或太短，<200 汉字不给稳定分数），
不是「有几条没过」；具体过没过看 `passed / total` 与 `--json` 的明细。

## 作者包长什么样

一个目录，一个作者：

```
voices/panchen-voice/
  pack.json                 # schema / slug / author / profile 的相对路径（corpus 可选）
  profile.json              # build 算出来的 42 条基线
  corpus/*.md               # 可选：语料，一篇一个文件；只有 build 需要，lint/report 不用
  judge_baseline.json       # 可选：判官基线
  exemplar_baseline.json    # 可选：样例基线
```

只发基线、不发语料的包（比如产品里内置的作者包）是合法包：`pack.json` 里不写 `corpus` 即可。
这种包能跑 `lint` / `report`；要跑 `judge` / `eval` 这类读作者原文的命令，必须给带语料的包（或显式 `--corpus`），否则会报「找不到 10 篇样例正文」。
`draft` 目前仍从仓库内置的 `voices/panchen-voice/` 取作者腔（已知缺陷：还不认 `--pack`，装 wheel 后跑不了），下一个版本修。

查找顺序：`--pack` → 环境变量 `VOICEKIT_PACK` → `./voices/*/pack.json`。

## 它诚实的地方

- **42 条属性都从语料里数出来**，不是拍脑袋定的。但也只覆盖"可数的"那一层。
- **分数不是及格线**。作者签过字的老稿也常掉分；论文、说理、公号这些语料里没有的体裁，低分是预期的，不是稿子差。
- **不许拿分数当放行标准**。判据只作诊断，签字由人。
- 生成这一层我们试过微调 3B 模型、各种配方：10 篇里 0 篇过关。它现在的定位不是替身。
- `judge.py` 的"同人判定"是 LLM 判官，**比人不可靠**（同一条判定重复跑四次，21/21 和 19/21 各两次）。
  证据层级：作者亲手判 > 可量数字 > LLM 判官。

## 换模型

默认 `deepseek`。也可以 `openai`、`moonshot`，或用环境变量接任何 OpenAI 兼容接口：

```bash
set VOICEKIT_BASE_URL=https://my-endpoint/v1
set VOICEKIT_API_KEY=...
set VOICEKIT_MODEL=my-model
```

## 开发

```bash
py -3.10 -m pytest tests -q
set VOICEKIT_TEST_PACK=voices/panchen-voice   # 让真包集成测试也跑
```

## 协议

MIT，见 [LICENSE](LICENSE)。
