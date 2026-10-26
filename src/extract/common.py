# -*- coding: utf-8 -*-
"""双模型标注共享代码：prompt、带重试的 LLM 调用。

只读复用 test_extract.py 的 SYSTEM_PROMPT 与 validate()，不修改其行为。
"""
import os
import re
import time
from pathlib import Path

import requests

from test_extract import SYSTEM_PROMPT, validate  # noqa: F401  （validate 再导出）

# 在既有 prompt 基础上补充日期精度规则（不改 test_extract.py 本体）
ANNOTATION_SYSTEM_PROMPT = SYSTEM_PROMPT + (
    "\n5. effective_date 的精度不得超过原文明确程度：原文只提到年份就填 YYYY，"
    "只到月份填 YYYY-MM，明确到日才填 YYYY-MM-DD，严禁虚构日/月级精度"
)

# 可重试的 HTTP 状态码（中转站不稳定：429 限流 / 5xx 网关错误）
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_ENV_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def load_api_config(path):
    """读取 API 配置；api_key 支持 ${ENV_VAR} 形式从环境变量解析（明文 key 不入库）。"""
    import yaml
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    m = _ENV_RE.match(str(cfg.get("api_key", "")))
    if m:
        key = os.environ.get(m.group(1))
        if not key:
            raise RuntimeError(
                f"环境变量 {m.group(1)} 未设置（{path} 的 api_key 引用它；"
                f"明文 key 不入库，请先 setx/export 该变量）")
        cfg["api_key"] = key
    return cfg


def call_llm(cfg, model, system_prompt, user, max_retries=4):
    """调用中转站 chat/completions，指数退避重试。

    最多重试 max_retries 次（即最多 1 + max_retries 次尝试）。
    返回 (text, elapsed, usage)；最终失败抛出最后一次异常。
    """
    last_err = None
    elapsed = 0.0
    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            r = requests.post(
                f"{cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}",
                         "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=cfg.get("timeout", 120),
            )
            elapsed = time.time() - t0
            if r.status_code in RETRYABLE_STATUS:
                raise RetryableError(f"HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            data = r.json()
            return (data["choices"][0]["message"]["content"],
                    elapsed, data.get("usage", {}))
        except (requests.RequestException, RetryableError, KeyError, ValueError) as e:
            elapsed = time.time() - t0
            last_err = e
            if attempt < max_retries:
                wait = (2 ** attempt) * 2 + 1  # 3s, 5s, 9s, 17s
                time.sleep(wait)
    raise last_err


class RetryableError(Exception):
    pass
