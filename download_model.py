"""임베딩 모델을 models/ 에 직접 받기 (느리거나 자주 끊기는 네트워크용, 이어받기 지원).

    uv run python download_model.py [intfloat/multilingual-e5-small]

- 끊기면 다시 실행하면 이어받는다.
- 받은 models/<이름>/ 폴더는 USB 등으로 다른 PC 의 같은 위치에 복사해도 된다.
- retriever 는 models/<이름>/ 이 있으면 인터넷 없이 그걸 쓴다.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

FILES = ["config.json", "modules.json", "sentence_bert_config.json", "special_tokens_map.json",
         "tokenizer_config.json", "tokenizer.json", "sentencepiece.bpe.model",
         "1_Pooling/config.json", "model.safetensors"]
MODELS = Path(__file__).parent / "models"


def get(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1000):
        have = part.stat().st_size if part.exists() else 0
        try:
            with requests.get(url, headers={"Range": f"bytes={have}-"} if have else {},
                              stream=True, timeout=(10, 30)) as r:
                if r.status_code == 416:  # 이미 다 받음
                    break
                r.raise_for_status()
                total = have + int(r.headers.get("content-length", 0))
                mode = "ab" if r.status_code == 206 else "wb"
                with part.open(mode) as f:
                    t0, n0 = time.time(), part.stat().st_size if mode == "ab" else 0
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                        if time.time() - t0 > 5:
                            done = f.tell()
                            print(f"  {dest.name}: {done >> 20}/{total >> 20}MB "
                                  f"({(done - n0) / (time.time() - t0) / 1024:.0f}KB/s)", flush=True)
                            t0, n0 = time.time(), done
            break
        except (requests.RequestException, OSError) as e:
            print(f"  끊김, 재시도 ({type(e).__name__})", flush=True)
            time.sleep(2)
    part.replace(dest)


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "intfloat/multilingual-e5-small"
    root = MODELS / name.split("/")[-1]
    for f in FILES:
        dest = root / f
        if dest.exists():
            print(f"✓ {f}")
            continue
        print(f"↓ {f}", flush=True)
        get(f"https://huggingface.co/{name}/resolve/main/{f}", dest)
    print(f"완료 → {root}")


if __name__ == "__main__":
    main()
