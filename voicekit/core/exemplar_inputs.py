"""小样本裁判的「输入」层：清洗正文、取 10 篇样例、拼 user prompt（FIX-355 拆出）。

从 `exemplar_judge.py` 分出来只为把主模块压在 1000 行以内（AGENTS 设计原则 13）；
提示正文与判定协议仍在 `exemplar_judge.py`，这里不持有状态。
"""

from __future__ import annotations

from pathlib import Path

from . import judge_lib as jl
from .exemplar_measured import sha256 as _sha256

lib = jl.lib
DEFAULT_CORPUS = jl.DEFAULT_CORPUS

_JSON_INSTRUCTION = (
    '{"verdict": "same|different|unsure", "reason": "一句理由", '
    '"cues": ["你依据的 1–3 个具体特征"]}'
)


def clean_body(text: str) -> str:
    """剥平台噪声，保留标题与作者自写内容。样例和待测稿走同一个清洗口径。"""
    body, _ = lib.clean_platform_noise(text)
    return body.strip()


def exemplar_texts(set_obj: dict, corpus: Path = DEFAULT_CORPUS) -> list[str]:
    texts = []
    for doc in set_obj["docs"]:
        path = corpus / str(doc["name"])
        body = clean_body(path.read_text(encoding="utf-8"))
        if _sha256(body) != doc.get("sha256"):
            raise jl.JudgeError(f"exemplar text changed: {doc['name']}")
        texts.append(body)
    return texts


def render_user_prompt(exemplars: list[str], draft: str) -> str:
    blocks = [f"### 样例 {index + 1}\n{text}" for index, text in enumerate(exemplars)]
    return (
        "## 这位作者的 10 篇随笔样例\n\n"
        + "\n\n".join(blocks)
        + "\n\n## 待测文章\n\n"
        + draft
        + "\n\n## 输出 JSON\n"
        + _JSON_INSTRUCTION
    )
