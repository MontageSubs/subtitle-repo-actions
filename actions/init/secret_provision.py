#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# Name: secret_provision.py
# Version: 1.0.1
# Organization: MontageSubs (蒙太奇字幕组)
# License: MIT License
# Source: https://github.com/MontageSubs/subtitle-repo-actions/actions/init/
#
# Description / 描述:
#    仅供初始化阶段调用一次：把组织级 secret 当前的值复制为该仓库自身的
#    仓库级 secret，使仓库切换为 private 后（脱离免费组织"org secret 仅
#    公开仓库可用"的限制）仍能在后续 workflow 中读到这些值。本模块从不
#    读取组织 secret 本身（GitHub API 不支持读取 secret 明文），只是转发
#    调用方进程当前环境里已经解密可见的值——这些值之所以可见，正是因为
#    初始化 workflow 运行时仓库还是 public，secrets.X 上下文能正常展开。
#    Called exactly once, during repository initialization: copies the
#    current values of organization-level secrets into the repository's
#    own repository-level secrets, so that after the repo switches to
#    private (losing free-tier org-secret access), later workflow runs can
#    still read them. This module never reads an org secret's value via
#    the API (GitHub never exposes secret plaintext); it forwards whatever
#    is already visible in the calling process's own environment, visible
#    only because the init workflow runs while the repo is still public.
#
# Overwrite semantics / 覆盖语义:
#    每个 secret 名称一律无条件 PUT（新建或覆盖），不做"先查是否已存在"
#    的判断——GitHub secrets API 是只写接口，无法比对已存值与待写值是否
#    相同，判断存在与否也无助于决定是否该覆盖。本模块只在初始化/强制
#    初始化（显式的用户触发重同步）时执行，无条件覆盖是唯一自洽的行为。
#    Every provisioned name is unconditionally PUT (create-or-update); no
#    existence check is attempted first — the secrets API is write-only
#    and cannot reveal whether a stored value differs from the incoming
#    one, so an existence check wouldn't inform the overwrite decision
#    anyway. Since this only runs on an explicit, user-triggered init or
#    force-init resync, unconditional overwrite is the correct behavior.
#
# Dependencies / 依赖:
#    - pynacl (pip install pynacl)
# ============================================================================
import subprocess
import sys
from base64 import b64encode

from github_api import call_api, is_debug, requires_org_admin_token

try:
    from nacl import encoding, public
    BOOTSTRAP_ERROR = None
except ImportError:
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pynacl"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        from nacl import encoding, public
        BOOTSTRAP_ERROR = None
    except Exception as e:
        public = None
        BOOTSTRAP_ERROR = str(e)

SCRIPT_NAME = "secret_provision"
SECRETS_API = "https://api.github.com/repos/{full_name}/actions/secrets"


def log(message):
    print(f"{SCRIPT_NAME}: {message}", file=sys.stderr)


def encrypt_secret(public_key_b64, secret_value):
    key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(key).encrypt(secret_value.encode("utf-8"))
    return b64encode(sealed).decode("utf-8")


@requires_org_admin_token("仓库级secret下沉", default=dict)
def provision(github_repository, github_token, secrets):
    if public is None:
        log(f"pynacl unavailable, skipping secret provisioning ({BOOTSTRAP_ERROR})")
        return {}

    secrets = {name: value for name, value in secrets.items() if value}
    if not secrets:
        log("no non-empty secret values to provision, skipping")
        return {}

    base_url = SECRETS_API.format(full_name=github_repository)
    ok, key_body = call_api(f"{base_url}/public-key", github_token)
    if not ok:
        log(f"failed to fetch repo public key: {key_body}")
        return {name: False for name in secrets}

    results = {}
    for name, value in secrets.items():
        encrypted_value = encrypt_secret(key_body["key"], value)
        ok, body = call_api(f"{base_url}/{name}", github_token, "PUT", {
            "encrypted_value": encrypted_value,
            "key_id": key_body["key_id"],
        })
        results[name] = ok
        if is_debug():
            log(f"{'provisioned' if ok else 'failed'}: {name}" + ("" if ok else f" ({body})"))
    succeeded = sum(1 for ok in results.values() if ok)
    log(f"provisioned {succeeded}/{len(results)} secret token(s)")
    return results
