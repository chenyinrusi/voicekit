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

## 六条命令

```bash
# 0. 没有作者包，只想先量一量这篇稿子（0 元，不需要任何配置）
voicekit quick 我的稿子.md
type 我的稿子.md | voicekit quick -          # 也能读管道

# 1. 体检一篇稿子（0 元）
voicekit lint 我的稿子.md --pack voices/panchen-voice
voicekit lint 我的稿子.md --pack voices/panchen-voice --annotate   # 每条不过的，指到 ¶段 句N

# 2. 一页 HTML 报告：结论 3 行 + 先改这三条 + 全部 42 条（0 元）
voicekit report 我的稿子.md --pack voices/panchen-voice

# 3. 从一个语料目录提炼作者 profile（一篇一个 .md）
voicekit build --corpus 语料/ --out voices/某人-voice --slug 某人 --author 某人

# 4. 按作者的腔起稿（要 key）
set DEEPSEEK_API_KEY=...        # PowerShell: $env:DEEPSEEK_API_KEY="..."
voicekit draft --material 材料.md --pack voices/panchen-voice --dry-run   # 先看提示词，0 元
voicekit draft --material 材料.md --pack voices/panchen-voice --out 初稿.md

# 5. 判同人：拿 10 篇作者样例判这篇像不像同一个人（要 key，只作参考）
voicekit judge 我的稿子.md --pack voices/panchen-voice --dry-run   # 0 元：只看提示词规模
voicekit judge 我的稿子.md --pack voices/panchen-voice
```

`--material` 要一个**材料文件**（一行一条事实），不是一句话；写成一句话会立刻报错，`rc=2`，不花钱。
`draft` 要读作者腔：不给 `--pack` 就按下面的查找顺序找，找不到就报人话退出（`rc=2`），不会去猜仓库路径。
包里没有 `corpus` 时，提示词里「他现在的腔」那节留空，其余照常。

`judge` 退出码：`0` 像 / `1` 不像 / `2` 判不了或环境错（缺基线、基线过期、包与样例集不匹配）。
它要读作者原文，所以包里必须有 `corpus`（或样例集里记的 corpus 目录）。判定会打印 tokens 与 `cost_usd`；
**它说像不等于作者肯签字**，放行仍由人拍板。

`quick` 退出码：`0` 量完了（汉字 <200 会多一行提醒，但照样给数）/ `2` 文件读不了、空文件、没有汉字。
它**没有作者基线**，所以不给分、不判像不像谁；`--json` 里明写 `"no_author_baseline": true`。

`lint` 退出码看的是「像不像同一个人」（`0` 像 / `1` 不像 / `2` 读不了或太短，<200 汉字不给稳定分数），
不是「有几条没过」；具体过没过看 `passed / total` 与 `--json` 的明细。
`--annotate` 会在 `--json` 的每一行里加 `evidence` / `evidence_note`；`evidence_note` 长这样：
`¶3 句2「……原句……」`，`¶` 是段号（按去掉标题/表格后的正文数），`句` 是这段里的第几句。
全篇统计量的属性（句长分布、标点谱、句读节奏）会写 `无句级证据（全文统计）`，不硬指句子。

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
这种包能跑 `lint` / `report` / `draft`；要跑 `judge` 这类读作者原文的命令，必须给带语料的包，否则会报「找不到 10 篇样例正文」。
`judge` 的样例名单不写死：钉死的作者包用包内 `judge_baseline.json` 推，泛型包用 `exemplar_baseline.json` 里记的语料；
两种都不需要仓库里的 `voices/` 目录，所以装成 wheel、在任意目录都能跑。

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
