"""ASS 通用浏览器会话消费者；仅在受信进程内读取和投影，不缓存、不探测网站。"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

MAX_SESSION_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
IDENTIFIER = re.compile(r"[a-z][a-z0-9._-]{0,62}\Z")
NAME = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z")
TOKEN = re.compile(r"ass_([0-9a-f]{32})_[A-Za-z0-9_-]{43}\Z")
PRIMARY_ORIGIN = "https://www.goofish.com"
SAFE_HEADERS = {"user-agent", "accept", "accept-language", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"}


class AssCredentialsError(Exception):
    """固定来源错误；不携带 HTTP 对象、Cookie、Key 或响应正文。"""


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError
    return value


def _time(value, *, milliseconds=True):
    if not isinstance(value, str):
        raise ValueError
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None or (milliseconds and d.microsecond % 1000):
        raise ValueError
    return d.timestamp()


def _json(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError
            result[k] = v
        return result
    def invalid(_value):
        raise ValueError
    data = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(data, dict):
        raise ValueError
    return data


def _environment(value, depth=0):
    if depth > 5:
        raise ValueError
    if isinstance(value, dict):
        if len(value) > 40 or any(len(k) > 128 for k in value):
            raise ValueError
        for v in value.values():
            _environment(v, depth + 1)
    elif isinstance(value, list):
        if len(value) > 40:
            raise ValueError
        for v in value:
            _environment(v, depth + 1)
    elif isinstance(value, str):
        if len(value.encode("utf-8")) > 4096:
            raise ValueError
    elif value is not None and (type(value) not in (int, float, bool) or (type(value) is float and not math.isfinite(value))):
        raise ValueError


class _Cookie(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: str = Field(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}$")
    value: SecretStr = Field(max_length=16384, repr=False)
    domain: str = Field(min_length=1, max_length=254)
    path: str = Field(pattern=r"^/", max_length=1024)
    hostOnly: bool
    secure: bool
    httpOnly: bool
    sameSite: str
    session: bool
    storeId: str
    expirationDate: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class _Storage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    local: dict[str, SecretStr] = Field(repr=False)
    session: dict[str, SecretStr] = Field(repr=False)


class _Page(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    origin: str
    page_url: str = Field(max_length=2048)
    referrer: str | None = Field(default=None, max_length=2048)
    visibility_state: str = "unknown"
    env: dict = Field(repr=False)
    storage: _Storage = Field(repr=False)
    headers: dict[str, SecretStr] = Field(repr=False)
    headers_status: str


class _Bundle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    format: str
    schema_version: int
    capture_mode: str | None = None
    pages_observed_at: str | None = None
    page_baseline_id: str | None = None
    site_id: str
    account_id: str
    producer_id: str
    snapshot_id: str
    ordering_mode: str
    snapshot_created_at: str
    observed_at: str
    state: str
    reason: str | None = None
    source_origins: list[str] = Field(min_length=1, max_length=5)
    cookies: list[_Cookie] = Field(max_length=500, repr=False)
    pages: list[_Page] = Field(max_length=5, repr=False)


@dataclass(frozen=True, repr=False)
class AssSessionSettings:
    """服务端精确绑定；Key 只从文件读取，不接受任务/API中传入原始凭据。"""
    origin: str
    key_file: Path
    tenant_id: str
    secret_id: str
    secret_name: str
    site_id: str = "goofish"
    account_id: str = "main"
    allow_mixed: bool = False
    unspecified_same_site: str = "reject"

    @classmethod
    def from_env(cls) -> AssSessionSettings:
        """只在明确选择ASS来源时读取设置，缺失或格式错误不退回本地。"""
        try:
            mixed = os.getenv("ASS_ALLOW_MIXED", "false").lower()
            if mixed not in {"true", "false"}:
                raise ValueError
            value = cls(
                origin=os.environ["ASS_ORIGIN"].rstrip("/"),
                key_file=Path(os.environ["ASS_READ_KEY_FILE"]),
                tenant_id=os.environ["ASS_TENANT_ID"], secret_id=os.environ["ASS_SECRET_ID"],
                secret_name=os.environ["ASS_SECRET_NAME"], site_id=os.getenv("ASS_SITE_ID", "goofish"),
                account_id=os.getenv("ASS_ACCOUNT_ID", "main"), allow_mixed=mixed == "true",
                unspecified_same_site=os.getenv("ASS_UNSPECIFIED_SAMESITE", "reject"),
            )
            value.validate()
            return value
        except Exception:
            raise AssCredentialsError("ass_configuration_invalid") from None

    def validate(self) -> None:
        """拒绝跳转型地址、非HTTPS、非精确引用及未声明的兼容降级。"""
        u = urlsplit(self.origin)
        _ = u.port
        if (u.scheme != "https" or not u.hostname or u.username is not None or u.password is not None
                or u.path or u.query or u.fragment or any(ord(c) <= 32 for c in self.origin)
                or not self.key_file.is_absolute() or not NAME.fullmatch(self.secret_name)
                or not IDENTIFIER.fullmatch(self.site_id) or not IDENTIFIER.fullmatch(self.account_id)
                or type(self.allow_mixed) is not bool or self.unspecified_same_site not in {"reject", "lax"}):
            raise ValueError
        _uuid(self.tenant_id)
        _uuid(self.secret_id)


@dataclass(repr=False)
class BrowserSession:
    """敏感包只供当前浏览器尝试使用，repr不显示负载。"""
    _bundle: _Bundle = field(repr=False)
    _settings: AssSessionSettings = field(repr=False)
    observed_age_seconds: int = 0

    def browser_state(self, *, now: float | None = None) -> dict:
        """投影精确Cookie作用域与localStorage；不生成磁盘状态文件。"""
        now = time.time() if now is None else now
        cookies = []
        for c in self._bundle.cookies:
            if c.expirationDate is not None and c.expirationDate <= now:
                continue
            same_site = {"lax": "Lax", "strict": "Strict", "no_restriction": "None"}.get(c.sameSite)
            if c.sameSite == "unspecified" and self._settings.unspecified_same_site == "lax":
                same_site = "Lax"
            if same_site is None:
                raise AssCredentialsError("ass_cookie_samesite_unrepresentable")
            if same_site == "None" and not c.secure:
                raise AssCredentialsError("ass_cookie_samesite_unrepresentable")
            cookies.append({"name": c.name, "value": c.value.get_secret_value(),
                            "domain": c.domain.removeprefix(".") if c.hostOnly else "." + c.domain.removeprefix("."),
                            "path": c.path, "secure": c.secure, "httpOnly": c.httpOnly,
                            "sameSite": same_site, "expires": c.expirationDate if c.expirationDate is not None else -1})
        if not cookies:
            raise AssCredentialsError("ass_cookie_missing")
        return {"cookies": cookies, "origins": [{"origin": p.origin, "localStorage": [
            {"name": k, "value": v.get_secret_value()} for k, v in p.storage.local.items()
        ]} for p in self._bundle.pages]}

    def context_environment(self) -> dict:
        """只返回现有消费路径所需的固定环境和安全headers，不回放任意请求。"""
        p = self._bundle.pages[0]
        return {"env": p.env, "headers": {k: v.get_secret_value() for k, v in p.headers.items()}}

    def session_storage(self) -> dict[str, str]:
        """返回批准主页面的sessionStorage数据，不提供可执行脚本。"""
        return {k: v.get_secret_value() for k, v in self._bundle.pages[0].storage.session.items()}

    @property
    def mixed(self) -> bool:
        """标记不同时间组，不把新Cookie观察当作页面或登录刷新。"""
        return self._bundle.capture_mode == "mixed"


def parse_session(raw: str, settings: AssSessionSettings, *, now: float | None = None) -> BrowserSession:
    """验证通用schema1/2；严格拒绝旧协议、失效、错误身份和不可接受的混合数据。"""
    now = time.time() if now is None else now
    try:
        settings.validate()
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_SESSION_BYTES:
            raise ValueError
        data = _json(raw)
        if type(data.get("schema_version")) is not int or data["schema_version"] not in (1, 2):
            raise ValueError
        if data.get("format") == "ass-browser-session" and data.get("state") == "invalid":
            raise AssCredentialsError("ass_session_invalidated")
        b = _Bundle.model_validate(data)
        if (b.format != "ass-browser-session" or b.state != "captured" or b.reason is not None
                or b.ordering_mode != "snapshot_created_at" or b.site_id != settings.site_id
                or b.account_id != settings.account_id or b.source_origins != [PRIMARY_ORIGIN]):
            raise ValueError
        _uuid(b.producer_id)
        _uuid(b.snapshot_id)
        created, observed = _time(b.snapshot_created_at), _time(b.observed_at)
        if observed < created or max(created, observed) > now + 60:
            raise ValueError
        if b.schema_version == 1:
            if any(v is not None for v in (b.capture_mode, b.pages_observed_at, b.page_baseline_id)):
                raise ValueError
        else:
            if b.capture_mode not in {"complete", "mixed"}:
                raise ValueError
            page_time = _time(b.pages_observed_at)
            _uuid(b.page_baseline_id)
            if page_time > observed:
                raise ValueError
            if b.capture_mode == "complete" and (page_time != observed or b.page_baseline_id != b.snapshot_id):
                raise ValueError
            if b.capture_mode == "mixed":
                if page_time > created or b.page_baseline_id == b.snapshot_id:
                    raise ValueError
                if not settings.allow_mixed:
                    raise AssCredentialsError("ass_mixed_requires_opt_in")
        seen = set()
        host = urlsplit(PRIMARY_ORIGIN).hostname
        for c in b.cookies:
            domain = c.domain.removeprefix(".")
            identity = (c.name, domain, c.path, c.hostOnly)
            value = c.value.get_secret_value()
            if (identity in seen or c.storeId != "0" or c.sameSite not in {"lax", "strict", "no_restriction", "unspecified"}
                    or c.session != (c.expirationDate is None) or (c.hostOnly and c.domain.startswith("."))
                    or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain)
                    or any(not x or len(x) > 63 or x.startswith("-") or x.endswith("-") for x in domain.split("."))
                    or not (host == domain or (not c.hostOnly and host.endswith("." + domain)))
                    or len(value.encode("utf-8")) > 16384 or any(ord(x) < 32 or ord(x) == 127 for x in value)):
                raise ValueError
            seen.add(identity)
        if [p.origin for p in b.pages] != [PRIMARY_ORIGIN]:
            raise ValueError
        for p in b.pages:
            if p.visibility_state not in {"visible", "hidden", "unknown"}:
                raise ValueError
            for value in (p.page_url, p.referrer):
                if value is None:
                    continue
                u = urlsplit(value)
                if (u.scheme + "://" + u.netloc != p.origin or u.query or u.fragment
                        or u.username is not None or u.password is not None or any(ord(x) <= 32 for x in value)):
                    raise ValueError
            if set(p.env) != {"navigator", "screen", "intl"} or any(not isinstance(v, dict) for v in p.env.values()):
                raise ValueError
            _environment(p.env)
            for entries in (p.storage.local, p.storage.session):
                if len(entries) > 500 or any(len(k.encode("utf-8")) > 512 or len(v.get_secret_value().encode("utf-8")) > MAX_SESSION_BYTES for k, v in entries.items()):
                    raise ValueError
            if p.headers_status not in {"observed", "not_observed"} or set(p.headers) - SAFE_HEADERS or (p.headers_status == "not_observed" and p.headers):
                raise ValueError
            if any(len(v.get_secret_value().encode("utf-8")) > 2048 or "\r" in v.get_secret_value() or "\n" in v.get_secret_value() for v in p.headers.values()):
                raise ValueError
        result = BrowserSession(b, settings, max(0, int(now - observed)))
        result.browser_state(now=now)  # 浏览器启动前检查是否能无损表达，禁止静默丢Cookie。
        return result
    except AssCredentialsError:
        raise
    except Exception:
        raise AssCredentialsError("ass_session_invalid") from None


class AssSessionClient:
    """每次尝试重新读取受保护Key，并以metadata→value→metadata固定版本。"""
    def __init__(self, settings: AssSessionSettings):
        self._settings = settings

    def read(self) -> BrowserSession:
        """只访问固定ASS origin；Key、value、storage不进入日志/文件或API返回。"""
        s = self._settings
        try:
            s.validate()
        except Exception:
            raise AssCredentialsError("ass_configuration_invalid") from None
        try:
            if not hasattr(os, "O_NOFOLLOW"):
                raise AssCredentialsError("ass_key_file_invalid")
            fd = os.open(s.key_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as f:
                info = os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o177:
                    raise AssCredentialsError("ass_key_file_invalid")
                token = f.read(130).removesuffix(b"\n").decode("ascii")
            match = TOKEN.fullmatch(token)
            if not match:
                raise AssCredentialsError("ass_key_file_invalid")
        except AssCredentialsError:
            raise
        except Exception:
            raise AssCredentialsError("ass_key_file_invalid") from None
        deadline = time.monotonic() + 20
        try:
            with httpx.Client(verify=True, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(5, connect=2), headers={"Authorization": "Bearer " + token, "Accept": "application/json"}) as client:
                def get(path):
                    client.cookies.clear()
                    raw = bytearray()
                    if time.monotonic() >= deadline:
                        raise AssCredentialsError("ass_unavailable")
                    with client.stream("GET", s.origin + path) as response:
                        if response.status_code != 200:
                            code = {401: "ass_authentication_rejected", 403: "ass_access_denied", 404: "ass_secret_unavailable", 429: "ass_rate_limited"}.get(response.status_code, "ass_unavailable")
                            raise AssCredentialsError(code)
                        limit = MAX_RESPONSE_BYTES if path.endswith("/value") else 20 * 1024
                        for chunk in response.iter_bytes():
                            if len(raw) + len(chunk) > limit or time.monotonic() > deadline:
                                raise AssCredentialsError("ass_response_invalid")
                            raw.extend(chunk)
                    return _json(raw.decode("utf-8"))
                status = get("/api/v1/secrets/access-status")
                now = time.time()
                if (status.get("tenant_id") != s.tenant_id or status.get("key_id") != str(UUID(match[1]))
                        or status.get("value_scope_authorized") is not True or type(status.get("revision")) is not int or status["revision"] < 1
                        or type(status.get("active_grant_count")) is not int or status["active_grant_count"] != 1
                        or not -5 <= now - _time(status.get("checked_at"), milliseconds=False) <= 60
                        or (status.get("expires_at") is not None and _time(status["expires_at"], milliseconds=False) <= now)):
                    raise AssCredentialsError("ass_identity_mismatch")
                path = "/api/v1/secrets/" + s.secret_name
                before = get(path + "/metadata")
                version = before.get("current_version")
                if before.get("id") != s.secret_id or before.get("name") != s.secret_name or before.get("tenant_id") != s.tenant_id or type(version) is not int or version < 1:
                    raise AssCredentialsError("ass_binding_changed")
                value = get(path + "/value")
                after = get(path + "/metadata")
                if after != before or value.get("name") != s.secret_name or type(value.get("version")) is not int or value["version"] != version:
                    raise AssCredentialsError("ass_binding_changed")
                return parse_session(value.get("value"), s, now=time.time())
        except AssCredentialsError:
            raise
        except Exception:
            raise AssCredentialsError("ass_unavailable") from None
