"""/terraform — diagram → Terraform 변환. terraform CLI 가 있으면 fmt·validate 까지 확인 (TERRAFORM_BIN 으로 경로 지정 가능)."""
import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from schemas import ArchitectureDraft, Constraints
from terraform_gen import generate

SAMPLES = Path(__file__).parent.parent / "data" / "samples"
TF = os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")


def load(name):
    return ArchitectureDraft.model_validate(json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8")))


def test_3tier_resources_follow_diagram_specs():
    a = load("architecture_3tier")
    out = generate(a.diagram, a.name)
    main = out["files"]["main.tf"]
    assert set(out["files"]) == {"versions.tf", "variables.tf", "main.tf", "outputs.tf", "README.md"}
    # 도식의 사양 그대로 (비용 계산과 같은 값)
    assert '"m7g.large"' in main and '"m7g.xlarge"' in main
    assert 'instance_class              = "db.r6g.xlarge"' in main and "multi_az                    = true" in main
    assert 'node_type                  = "cache.r7g.large"' in main and 'engine                     = "valkey"' in main
    assert "al2023-ami-kernel-default-arm64" in main          # Graviton → arm64 AMI
    # 보안 기본값
    assert "storage_encrypted           = true" in main and 'http_tokens = "required"' in main
    assert "block_public_acls       = true" in main and "manage_master_user_password = true" in main
    # 연결선 → 보안 그룹 규칙 (app → rds 3306)
    assert 'resource "aws_vpc_security_group_ingress_rule" "rds_from_app"' in main
    assert "from_port                    = 3306" in main
    # CloudFront 용 WAF 는 us-east-1
    assert re.search(r'scope\s+= "CLOUDFRONT"', main) and re.search(r'alias\s+= "us_east_1"', out["files"]["versions.tf"])


def test_frozen_resource_is_protected_not_recreated_silently():
    a = load("architecture_3tier")
    out = generate(a.diagram, a.name, Constraints(frozen_resources=["RDS prod-db MySQL 8.0 유지"]))
    main = out["files"]["main.tf"]
    assert "# [변경 금지] RDS prod-db MySQL 8.0 유지" in main
    assert "terraform import aws_db_instance.rds" in main and "prevent_destroy = true" in main


def test_serverless_without_vpc():
    a = load("architecture_serverless")
    out = generate(a.diagram, a.name)
    main = out["files"]["main.tf"]
    assert "aws_vpc" not in main and "aws_lambda_function" in main
    assert any("Lambda" in w for w in out["warnings"])


def test_korean_only_name_falls_back():
    a = load("architecture_serverless")
    assert generate(a.diagram, "서버리스 전환안")["project"] == "kkari"
    assert generate(a.diagram, "Graviton ASG + RDS")["project"] == "graviton-asg-rds"


def test_endpoint_json_and_zip(client):
    a = load("architecture_3tier").model_dump()
    r = client.post("/terraform", json=a)  # /architectures 의 architectures[i] 를 그대로 보내도 됨
    assert r.status_code == 200 and "main.tf" in r.json()["files"]
    r = client.post("/terraform", json={**a, "format": "zip"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "terraform.zip" in r.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "terraform/main.tf" in names and "terraform/README.md" in names


def test_endpoint_rejects_bad_diagram(client):
    r = client.post("/terraform", json={"name": "x", "diagram": {"zones": []}})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.skipif(not TF, reason="terraform CLI 없음 (TERRAFORM_BIN 으로 지정 가능)")
@pytest.mark.parametrize("sample", ["architecture_3tier", "architecture_serverless"])
def test_terraform_fmt_and_validate(sample, tmp_path):
    a = load(sample)
    c = Constraints(contains_personal_data=True, frozen_resources=["RDS prod-db"])
    for name, text in generate(a.diagram, a.name, c)["files"].items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    env = {**os.environ, "TF_PLUGIN_CACHE_DIR": os.environ.get("TF_PLUGIN_CACHE_DIR", str(Path.home() / ".terraform.d" / "plugin-cache"))}
    Path(env["TF_PLUGIN_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run([TF, *args], cwd=tmp_path, env=env, capture_output=True, text=True)  # noqa: E731
    fmt = run("fmt", "-check", "-diff")
    assert fmt.returncode == 0, fmt.stdout
    init = run("init", "-backend=false", "-input=false", "-no-color")
    if init.returncode != 0:
        pytest.skip("terraform init 실패 (네트워크): " + init.stderr[-200:])
    val = run("validate", "-no-color")
    assert val.returncode == 0, val.stdout + val.stderr
