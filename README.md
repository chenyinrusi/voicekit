# voicekit

把一个人的文风变成**能数出来的判据**，然后拿这些判据去体检稿子、按他的腔起稿。

不是"AI 替你写"。是**起稿机 + 挑刺器**：先给你一个像他的草稿，再逐条告诉你哪里不像。
放不放行永远是作者签字，不是分数。

## 装

```bash
py -3.10 -m pip install writer-voice     # 发行名是 writer-voice，装完命令叫 voicekit
```

装用 PyPI 包名 `writer-voice`；模块名和命令名都是 `voicekit`。以后选名字时这点说清楚省得找错。
下面所有命令都写 `py -3.10 -m voicekit`：Windows 上 `pip` 不一定是 3.10 那个。

本地开发：

```bash
py -3.10 -m pip install -e .
```

**包不在 wheel 里**：`--pack` 要指向你本地的作者包目录（自己 `build` 出来的，或别人给的）。
装完先跑 `quick` 不需要包；`lint` / `report` / `draft` / `judge` 都要 `--pack`。

只依赖标准库 + PyYAML。`lint` / `report` 不花钱（不调模型）。`build` 只在 `style_distill` 环节要模型；
`draft` 和 `judge` 要模型。

## 六条命令

```bash
# 0. 没有作者包，只想先量一量这篇稿子（0 元，不需要任何配置）
voicekit quick 我的稿子.md
type 我的稿子.md | voicekit quick -          # 也能读管道

# 1. 体检一篇稿子（0 元）
voicekit lint 我的稿子.md --pack voices/panchen-voice
voicekit lint 我的稿子.md --pack voices/panchen-voice --annotate   # 能指到句的，给 ¶段 句N

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

`judge` 退出码：`0` 像 / `1` 不像 / `2` 判不了或环境错（稿子 <200 汉字、缺基线、基线过期、
包与样例集不匹配、模型换过）。样例正文优先读包里的 `corpus`，没有就读样例集记的目录
（相对路径按 cwd → 包目录 → 包目录往上三层找）。判定会打印 tokens 与 `cost_usd`；
**它说像不等于作者肯签字**，放行仍由人拍板。
判定缓存默认放 `~/.cache/voicekit/judge`，可用 `VOICEKIT_JUDGE_CACHE` 改；同一稿同一基线命中缓存不花钱。
缓存只存判定结果（按提示词 sha256 命名），不存稿件正文；`--no-cache` 可强制重判。
模型输出不能用时会自动再判一次，并打印 `[retry 1/1]`；重试也算 token，`--retries 0` 可关掉（默认 1）。
真跑一次就追加一行用量到 `<当前项目>/logs/voicekit_usage.jsonl`（往上找最近带 `.git` 的目录；不在项目里跑才落 `~/.cache/voicekit/usage.jsonl`），可用 `VOICEKIT_USAGE_LOG` 改路径（设空串 = 不记）。
起稿也记同一本账；账本写不进去只警告，不影响结论。

`draft` 退出码：`0` 写了 / `1` 事实有问题 / `2` 缺包、读不了、参数错。
真跑完会打印 tokens 与 `cost_usd`，并记一行到同一本用量台账。
`report`（含 `lint --html`）碰到读不了的文件也是 `rc=2`，不会漏成 `1`。
`--dry-run` 会把能查的都查一遍（含模型与样例集是否匹配、稿子是否太短），所以预检绿了真跑就不会断。

`quick` 退出码：`0` 量完了（汉字 <200 会多一行提醒，但照样给数）/ `2` 文件读不了、空文件、没有汉字。
它**没有作者基线**，所以不给分、不判像不像谁；`--json` 里明写 `"no_author_baseline": true`。

`lint` 退出码看的是「像不像同一个人」（`0` 像 / `1` 不像 / `2` 读不了或太短，<200 汉字不给稳定分数），
不是「有几条没过」；具体过没过看 `passed / total` 与 `--json` 的明细。
`--annotate` 只给**不过的那几行**加证据：`--json` 里多 `evidence` / `evidence_note`（过的行不加），
人类输出末尾多一段 `-- 句级证据（¶段 句N，只标位置，不判好坏）`。`evidence_note` 长这样：
`¶3 句2「……原句……」`。`¶` 是清洗后正文的段号：空行分段，一行也算一段，
H1 标题行、表格行也各占一个段号（和 `measure()` 数段同一套口径，不为报告另数一遍）；
`句` 是这段里第几句。42 条里 21 条能指到句（19 条词表 + 碎片句 + 问句/感叹句），
其余 21 条是全篇统计量（句长分布、标点谱、句读节奏、句尾模式、TTR 类），写
`无句级证据（全文统计）`，不硬指句子。证据只标位置、不判好坏：指标偏低时它写
「全文共 N 处」，意思是「全文就这几处」，不是说删掉这些句子。

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
这种包能跑 `lint` / `report` / `draft`；`judge` 还要 10 篇样例正文，顺序是：
包里的 `corpus` → 样例集 `exemplar_baseline.json` 记的目录 → 两边都没有才报「找不到 10 篇样例正文」。
`judge` 的样例名单不写死：钉死的作者包用包内 `judge_baseline.json` 推，泛型包用 `exemplar_baseline.json` 里记的语料；
两种都不需要仓库里的 `voices/` 目录，所以装成 wheel、在任意目录都能跑。
**钉死包的样例集里记的相对路径是相对仓库根的**：把包拷到别处时，要么一起拷语料，要么改那条路径。

`weights.json`（可选）放加权表：`score_weighted` 按它算，`score`（放行参照）仍等权。
带 `weights.json` 的包，装成 wheel 和从源码跑，`score_weighted` 必须一样。

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
