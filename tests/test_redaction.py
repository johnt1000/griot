"""Credential-looking values are replaced before text is embedded or stored.

The values below are assembled at run time from pieces. Written whole, this
file would look like a leak to the repository's own secret scanner, and to
anyone's who vendors it."""

import pytest

from griot import redaction


def T(*parts: str) -> str:
    return "".join(parts)


AGE_BODY = T("QPZRY9X8GF2TVDW0", "S3JN54KHCE6MUA7L", "QPZRY9X8GF2TVDW0", "S3JN54KHCE")

# rule -> (text containing a credential-shaped value, the value itself)
POSITIVE = {
    "private-key": (T("-----BEGIN RSA ", "PRIVATE KEY-----\nMIIEpAIBAAKCAQEA7\nabcDEF123\n-----END RSA ", "PRIVATE KEY-----"),
                    "MIIEpAIBAAKCAQEA7"),
    "aws-access-key-id": ("aws_access_key_id = " + T("AKIA", "IOSFODNN7", "EXAMPLE"), T("AKIA", "IOSFODNN7", "EXAMPLE")),
    "aws-secret-access-key": ("aws_secret_access_key = " + T("wJalrXUtnFEMI/K7MDENG", "/bPxRfiCYEXAMPLEKEY"),
                              T("wJalrXUtnFEMI/K7MDENG", "/bPxRfiCYEXAMPLEKEY")),
    "github-token": ("token: " + T("ghp_", "A1b2C3d4E5f6G7h8I9j0", "K1l2M3n4O5p6Q7r8"), T("ghp_", "A1b2C3d4E5f6G7h8I9j0", "K1l2M3n4O5p6Q7r8")),
    "gitlab-token": ("runner " + T("glrt-", "Ab1Cd2Ef3Gh4Ij5Kl6Mn") + " and GITLAB=" + T("glpat-", "Ab1Cd2Ef3Gh4Ij5Kl6Mn"), T("glpat-", "Ab1Cd2Ef3Gh4Ij5Kl6Mn")),
    "slack-token": ("slack " + T("xoxb-", "123456789012-", "abcdefABCDEF"), T("xoxb-", "123456789012-", "abcdefABCDEF")),
    "stripe-key": ("stripe " + T("sk_", "live_", "4eC39HqLyjWDarjtT1zdp7dc"), T("sk_", "live_", "4eC39HqLyjWDarjtT1zdp7dc")),
    "openai-key": ("OPENAI " + T("sk-", "proj-", "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2"), T("sk-", "proj-", "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2")),
    "anthropic-key": ("KEY " + T("sk-", "ant-", "api03-Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0"), T("sk-", "ant-", "api03-Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0")),
    "google-api-key": ("maps " + T("AIza", "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"), T("AIza", "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q")),
    "jwt": ("const anon = '" + T("eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U") + "'",
            T("eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxMjM0NTY3ODkwIn0")),
    "url-password": ("DATABASE_URL=postgres://admin:" + T("s3cr3t", "Pass9") + "@db.internal:5432/app", T("s3cr3t", "Pass9")),
    "bearer-token": ('curl -H "Authorization: Bearer ' + T("abc123DEF456", "ghi789JKL012mno") + '" https://api.example.com',
                     T("abc123DEF456", "ghi789JKL012mno")),
    "credential-literal": ('API_KEY = "' + T("9f8a7b6c5d4e", "3f2a1b0c9d8e7f6a") + '"', T("9f8a7b6c5d4e", "3f2a1b0c9d8e7f6a")),
    "credential-assignment": ("export GITHUB_DEPLOY_TOKEN=" + T("Zk9f2Qm7Xw4Lp1", "Rt8Vb3Nc6Hs5Jd0"), T("Zk9f2Qm7Xw4Lp1", "Rt8Vb3Nc6Hs5Jd0")),
    "basic-auth": ("Authorization: Basic " + T("dXNlcjpzM2NyM3RQ", "YXNzOTk5"), T("dXNlcjpzM2NyM3RQ", "YXNzOTk5")),
    "npm-token": ("//registry.npmjs.org/:_authToken=" + T("npm_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"), T("npm_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")),
    "pypi-token": ("password = " + T("pypi-", "AgEIcHlwaS5vcmc", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6"),
                   T("AgEIcHlwaS5vcmc", "A1b2C3d4E5f6G7h8I9j0")),
    "huggingface-token": ("HF=" + T("hf_", "AbCdEfGhIjKlMnOpQrStUvWxYz01234567"), T("hf_", "AbCdEfGhIjKlMnOpQrStUvWxYz01234567")),
    "sendgrid-key": ("SENDGRID " + T("SG.", "AbCdEfGhIjKlMnOpQrStUv", ".", "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfG"),
                     T("AbCdEfGhIjKlMnOpQrStUv", ".", "AbCdEfGhIjKlMn")),
    "digitalocean-token": ("DO " + T("dop_v1_", "0123456789abcdef" * 4), T("dop_v1_", "0123456789abcdef")),
    "slack-webhook": ("hook https://" + T("hooks.slack.com/services/", "T01ABCDEF/B01ABCDEF/", "AbCdEfGhIjKlMnOpQrStUvWx"),
                      T("T01ABCDEF/B01ABCDEF/", "AbCdEfGhIjKlMnOpQrStUvWx")),
    "discord-webhook": ("hook https://" + T("discord.com/api/webhooks/", "123456789012345678/", "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
                        T("123456789012345678/", "AbCdEfGhIjKlMnOp")),
    "google-oauth-secret": ("client_secret " + T("GOCSPX-", "AbCdEfGhIjKlMnOpQrStUvWxYz01"), T("GOCSPX-", "AbCdEfGhIjKlMnOpQrStUvWxYz01")),
    "shopify-token": ("SHOP " + T("shpat_", "0123456789abcdef" * 2), T("shpat_", "0123456789abcdef")),
    "age-secret-key": ("# identity\n" + T("AGE-SECRET-", "KEY-1") + AGE_BODY, AGE_BODY[:32]),
    "telegram-bot-token": ("BOT " + T("123456789:", "AAH", "dqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"), T("123456789:", "AAH", "dqTcvCH1vGWJxfSe")),
    "stripe-webhook-secret": ("endpoint " + T("whsec_", "AbCdEfGhIjKlMnOpQrStUvWxYz012345"), T("whsec_", "AbCdEfGhIjKlMnOpQrStUvWxYz012345")),
}


def test_every_detector_has_a_positive_case():
    assert {d.rule for d in redaction.DETECTORS} == set(POSITIVE)


@pytest.mark.parametrize("rule", list(POSITIVE))
def test_a_credential_shaped_value_is_replaced_and_the_rest_is_kept(rule):
    text, value = POSITIVE[rule]
    out, found = redaction.redact("before\n" + text + "\nafter")
    assert value not in out
    assert rule in found
    assert redaction.marker(rule) in out
    assert out.startswith("before\n") and out.endswith("\nafter")


# The formats that are recognised by a fixed prefix.
PREFIXED = ["aws-access-key-id", "github-token", "gitlab-token", "slack-token", "stripe-key", "stripe-webhook-secret",
            "anthropic-key", "openai-key", "google-api-key", "google-oauth-secret", "npm-token", "pypi-token",
            "huggingface-token", "sendgrid-key", "digitalocean-token", "shopify-token", "age-secret-key",
            "telegram-bot-token", "jwt"]


def _whole(rule: str) -> str:
    """The whole value the rule's own positive case holds (POSITIVE keeps
    only a piece of some: the piece that must not survive)."""
    detector = next(d for d in redaction.DETECTORS if d.rule == rule)
    return detector.pattern.search(POSITIVE[rule][0]).group(0)


@pytest.mark.parametrize("rule", PREFIXED)
@pytest.mark.parametrize("before", ["fix_", "backup-", "deploy/", "v1.", "KEY="])
def test_a_token_glued_to_what_comes_before_it_is_still_replaced(rule, before):
    """A branch named `fix_<token>`, a file `backup_<token>.sh`. The pattern
    began at a word boundary, and an underscore is a word character: after
    one, the token was not seen at all."""
    out, rules = redaction.redact(before + _whole(rule))
    assert POSITIVE[rule][1] not in out and rules


@pytest.mark.parametrize("rule", ["aws-access-key-id", "github-token", "stripe-key", "stripe-webhook-secret", "npm-token",
                                  "huggingface-token", "digitalocean-token", "shopify-token", "age-secret-key"])
def test_a_token_followed_by_an_underscore_is_still_replaced(rule):
    """`<token>_old`: the pattern ended at a word boundary too."""
    out, rules = redaction.redact("name " + _whole(rule) + "_old")
    assert POSITIVE[rule][1] not in out and rules


@pytest.mark.parametrize("rule", list(POSITIVE))
def test_running_it_again_changes_nothing(rule):
    once, _ = redaction.redact(POSITIVE[rule][0])
    twice, found = redaction.redact(once)
    assert twice == once and found == []


# Ordinary code and prose that mention credentials without containing one. A
# detector that fired here would blank out what people search for.
NEGATIVE = [
    "apiKey: process.env.SUPABASE_SERVICE_ROLE_KEY,",
    "token = request.headers.get('authorization')",
    "password = hash_password(raw_password)",
    'secret_name = "my_application_secret_name_here"',
    'API_KEY = "your-api-key-goes-here-please"',
    "const token = await getAccessTokenSilently();",
    "postgres://user:${DB_PASSWORD}@localhost:5432/app",
    "mysql://root:password@localhost/test",
    "redis://localhost:6379/0",
    "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
    'DATABASE_URL: "postgresql://test:test@localhost:5432/test"',
    "https://example.com/path?token=abc",
    "class sk-this-is-a-long-hyphenated-css-class-name-example {}",
    "Authorization: Bearer <your token here>",
    "Authorization: Bearer ${ACCESS_TOKEN}",
    "the AKIA prefix marks an access key id",
    "git commit 9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a fixes it",
    "sha256 = \"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"",
    "uuid: 123e4567-e89b-12d3-a456-426614174000",
    "eyJ is how a base64 JSON object starts",
    "-----BEGIN PUBLIC KEY-----\nMFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAK\n-----END PUBLIC KEY-----",
    "-----BEGIN CERTIFICATE-----\nMIIBkTCB+wIJAL\n-----END CERTIFICATE-----",
    "export const DEFAULT_PASSWORD_MIN_LENGTH = 12;",
    # A key header that is only MENTIONED: constants, docs, scanners of their own.
    'Its private key starts with `' + T("-----BEGIN OPENSSH ", "PRIVATE KEY-----") + '`.\n\n' + "More prose.\n" * 50,
    'PEM_HEAD = b"' + T("-----BEGIN ", "PRIVATE KEY-----") + '"\n' + "def f(): pass\n" * 40 + 'PEM_TAIL = b"' + T("-----END ", "PRIVATE KEY-----") + '"',
    'Compare with "' + T("-----BEGIN ", "PRIVATE KEY-----") + '" thequickbrownfoxjumps is just a word.',
    "header: " + T("-----BEGIN ", "PRIVATE KEY-----") + "\n\nDescriptionOfTheFunctionBelowIsLong and more text",
    # Identifiers and other written things that happen to sit next to a credential word.
    "'secret_ref': 'V1LocalObjectReference'",
    "token: createO200KSpecialTokenMap,",
    'TOKEN_STRINGS = "[A-Za-z0-9_-]{20,}and1more2"',
    '"token_here_1007": "分析器预览版本1和版本2的说明文字内容示例"',
    # A quoted value next to a credential word that is a NAME of something, or an identifier.
    'token_env = "GRIOT_CHAT_PRICE_PER_1M_TOKENS"',
    '"RAG_CHAT_PRICE_PER_1M_TOKENS": "GRIOT_CHAT_PRICE_PER_1M_TOKENS"',
    'auth_sha = "' + T("9f86d081884c7d65", "9a2feaa0c55ad015", "a3bf4f1b2b0b822c", "d15d6c15b0f00a08") + '"',
    'uuid_token = "' + T("123e4567-e89b-", "12d3-a456-", "426614174000") + '"',
    'api_key_header = "X-Api-Key-V2-Header-Name-20"',
    'password_hash = "' + T("5f4dcc3b5aa765d6", "1d8327deb882cf99") + '"',
    'private_key_id = "0123456789abcdef0123456789abcdef01234567"',
    # Unquoted, and not a value: expressions, identifiers, references.
    "token = generateToken32BytesForUser(user)",
    "authToken = userSessionToken2Value;",
    "password: ${{ secrets.DB_PASSWORD_V2_PRODUCTION }}",
    "API_KEY=$MY_SERVICE_API_KEY_V2_FROM_VAULT",
    "secret_key_base: <%= ENV['SECRET_KEY_BASE_2024_PROD'] %>",
    "token_expiry_seconds = 3600000000000000",
    # Placeholders inside URLs.
    "gist://username:TOKEN@gist.github.com/abc",
    "hdfs://username:pwd@node:9000/path",
    "smb://myuser:mypassword@server/share",
    "http://evil:thinking@example.com",
    "git://[path-to-repo[:]][ref]@path",
    "Authorization: Basic <base64 of user:password>",
    # Names that carry a digit: design tokens, cache keys, versioned identifiers.
    "{ token: '--wa-color-brand-500' }",
    'token: "color-primary-500-alpha-10"',
    'secret_key_base_name = "rails_master_key_2024_prod"',
    'apiKey: "my-service-api-key-v2-placeholder"',
    "password: string; token?: string; secret: Buffer",
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_ordinary_code_and_prose_are_left_exactly_as_they_are(text):
    out, found = redaction.redact(text)
    assert out == text and found == []


def test_a_private_key_cut_off_before_its_end_is_still_replaced_to_the_end():
    text = "config:\n" + T("-----BEGIN OPENSSH ", "PRIVATE KEY-----") + "\nb3BlbnNzaC1rZXktdjEAAAAA\nmore"
    out, found = redaction.redact(text)
    assert found == ["private-key"] and "b3BlbnNzaC1rZXk" not in out and out.startswith("config:\n")


def test_several_values_in_one_text_are_all_replaced():
    text = POSITIVE["github-token"][0] + "\n" + POSITIVE["jwt"][0] + "\n" + POSITIVE["url-password"][0]
    out, found = redaction.redact(text)
    assert sorted(found) == ["github-token", "jwt", "url-password"]
    for rule in ("github-token", "jwt", "url-password"):
        assert POSITIVE[rule][1] not in out


def test_only_the_password_of_a_url_is_replaced():
    out, _ = redaction.redact(POSITIVE["url-password"][0])
    assert out == "DATABASE_URL=postgres://admin:[REDACTED:url-password]@db.internal:5432/app"


def test_the_keyword_and_quotes_around_a_literal_are_kept():
    out, _ = redaction.redact(POSITIVE["credential-literal"][0])
    assert out == 'API_KEY = "[REDACTED:credential-literal]"'


def test_text_without_anything_is_returned_as_the_same_object_value():
    text = "def add(a, b):\n    return a + b\n"
    assert redaction.redact(text) == (text, [])


# --- a repository is untrusted input: no detector may be made to run forever ------------
# Each text is what a hostile (or merely minified) file could contain, shaped to make a
# careless pattern retry the same characters over and over.
PATHOLOGICAL = {
    "one long word": "a" * 60_000,
    "identifier characters": "a.b-c_" * 10_000,
    "keyword repeated": "token" * 12_000,
    "keyword and equals": "secret=" * 8_500,
    "jwt prefix": "eyJ" * 20_000,
    "scheme": "x://" * 15_000,
    "scheme and user": "ab://u:" * 8_500,
    "unterminated key blocks": (T("-----BEGIN RSA ", "PRIVATE KEY-----") + "\n" + "A" * 300) * 180,
    "key prefix": "sk-" * 20_000,
    "base64 run": "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" * 1_700,
    "bearer": "authorization: bearer " * 2_700,
    "aws prefix": "AKIA" * 15_000,
    "aws keyword": "aws_secret_access_key" * 2_800,
    "gitlab prefix": "glpat-" * 10_000,
    "password without an end": "ab://" + "u" * 60 + ":" + "p" * 59_000,
    "keyword then a long quoted value without a closing quote": 'api_key = "' + "A1b2" * 15_000,
    "keyword glued to itself": "apikey" * 10_000,
    "unquoted assignments": ("token=" + T("Zk9f2Qm7Xw", "4Lp1Rt8Vb3") + " ") * 2_300,
    "an unquoted value without an end": "password=" + "A1" * 30_000,
    "key headers without a body": (T("-----BEGIN RSA ", "PRIVATE KEY-----") + "\n") * 1_800,
    "armor headers without a body": T("-----BEGIN PGP ", "PRIVATE KEY BLOCK-----") + "\n" + "Version: x\n" * 5_000,
    "a key body of one endless line": T("-----BEGIN RSA ", "PRIVATE KEY-----") + "\n" + "QUJD" * 15_000,
    "a key body broken every 16 characters": T("-----BEGIN RSA ", "PRIVATE KEY-----") + "\n" + "QUJDREVGR0hJSktM\n" * 3_500,
    "a key body of indented short lines": T("-----BEGIN RSA ", "PRIVATE KEY-----") + "\n" + "        QUJD1\n" * 4_500,
    "slack webhook prefix": "hooks.slack.com/services/T" * 2_300,
    "discord webhook prefix": "discord.com/api/webhooks/1111111111111111/" * 1_400,
    "sendgrid dots": "SG." * 20_000,
    "telegram digits": "123456789:AA" * 5_000,
    "basic authorization": "authorization basic " * 3_000,
    "assignments with tabs": "token:\t\t" * 7_500,
    "a url user without an end": "ab://" + "a:" * 30_000,
}


# The child spends its own CPU on `setup` (importing griot is most of it) before
# the clock starts: the limit covers `call` alone.
_CPU_LIMITED_CHILD = """
import math, resource, sys
cpu_seconds = int(sys.argv[1])
{setup}
text = sys.stdin.read()
usage = resource.getrusage(resource.RUSAGE_SELF)
spent = math.ceil(usage.ru_utime + usage.ru_stime)
_, hard = resource.getrlimit(resource.RLIMIT_CPU)
resource.setrlimit(resource.RLIMIT_CPU, (spent + cpu_seconds, hard))
{call}
"""

# Only a backstop for a child that never gets CPU at all: the verdict comes
# from the CPU limit, which kills a runaway after `cpu_seconds` of CPU however
# long the machine makes it wait for them.
_WALL_BACKSTOP_SECONDS = 120


def _runs_away(setup: str, call: str, text: str, cpu_seconds: int) -> str | None:
    """Why `call` ran away on `text`, or None if it finished in time.

    Time is CPU time, not wall time: a backtracking pattern burns CPU, while
    a busy machine (the full suite, a CI runner) only makes a fast one wait
    for a CPU, and a wall-clock limit failed a fast detector that way
    (2026-10-09). RLIMIT_CPU makes the kernel kill the child with SIGXCPU
    once it has spent its budget, so a runaway still fails in about
    `cpu_seconds` instead of after minutes."""
    import signal
    import subprocess
    import sys
    # RLIMIT_CPU and SIGXCPU are POSIX: CI runs Linux and macOS, and a Windows
    # runner would skip these checks instead of failing on a missing module.
    pytest.importorskip("resource", reason="the CPU limit needs POSIX RLIMIT_CPU")
    child = _CPU_LIMITED_CHILD.format(setup=setup, call=call)
    try:
        done = subprocess.run([sys.executable, "-c", child, str(cpu_seconds)], input=text, text=True,
                              capture_output=True, timeout=_WALL_BACKSTOP_SECONDS)
    except subprocess.TimeoutExpired:
        return f"no verdict after {_WALL_BACKSTOP_SECONDS} seconds of wall time"
    if done.returncode == -signal.SIGXCPU:
        return f"still running after {cpu_seconds} seconds of CPU"
    # Any other failure is a broken check, not a fast detector: say so.
    assert done.returncode == 0, f"the child failed (exit {done.returncode}): {done.stderr[-2000:]}"
    return None


@pytest.mark.parametrize("name", list(PATHOLOGICAL))
def test_no_input_makes_the_detectors_run_away(name):
    """~60 KB each. A pattern that backtracks takes minutes on these; one that
    does not takes about 20 ms of CPU. The budget is 3 seconds of CPU, far
    below what backtracking costs and far above the real cost."""
    assert _runs_away("from griot import redaction", "redaction.redact(text)", PATHOLOGICAL[name], 3) is None


def test_the_runaway_check_catches_a_pattern_that_backtracks():
    """The check above is only worth something if it fails for a real
    catastrophic pattern, and once its CPU budget is spent: by the kernel's
    CPU limit, not by the wall-clock backstop minutes later."""
    why = _runs_away("import re; pattern = re.compile(r'(a+)+$')", "pattern.search(text)", "a" * 40 + "!", 1)
    assert why == "still running after 1 seconds of CPU"


def test_the_runaway_check_does_not_count_time_spent_off_the_cpu():
    """A loaded machine (the full suite, a CI runner) leaves the detector
    waiting for a CPU: a wall-clock limit then fails a fast pattern
    (2026-10-09). Sleeping past the budget is that wait, made certain."""
    assert _runs_away("import time", "time.sleep(2)", "", 1) is None


def test_a_child_that_fails_is_not_read_as_a_fast_detector():
    """A child that dies of something else (griot fails to import, say) also
    ends early: read as "finished in time", every case above would pass
    without having run a detector."""
    with pytest.raises(AssertionError, match="the child failed"):
        _runs_away("import griot.no_such_module", "pass", "", 1)


# --- private keys: the material, not the mention -------------------------------------------


def test_a_private_key_in_one_line_with_escaped_line_breaks_is_replaced():
    """How a service-account file or a YAML value carries one."""
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"
    text = '"private_key": "' + T("-----BEGIN ", "PRIVATE KEY-----") + "\\n" + body + "\\n" + body + "\\n" + T("-----END ", "PRIVATE KEY-----") + '\\n", "client_email": "x@example.com"'
    out, found = redaction.redact(text)
    assert found == ["private-key"] and body not in out
    assert out.endswith('"client_email": "x@example.com"'), "what follows the key is kept"


def test_an_encrypted_private_key_with_its_header_lines_is_replaced():
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"
    text = T("-----BEGIN RSA ", "PRIVATE KEY-----") + "\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0123456789ABCDEF\n\n" + body + "\n" + T("-----END RSA ", "PRIVATE KEY-----") + "\nafter"
    out, found = redaction.redact(text)
    assert found == ["private-key"] and body not in out and out.endswith("\nafter")


def test_text_after_a_cut_off_private_key_is_kept():
    body = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB"
    text = T("-----BEGIN OPENSSH ", "PRIVATE KEY-----") + "\n" + body + "\n\nThe rest of the document is about something else.\n" * 3
    out, found = redaction.redact(text)
    assert found == ["private-key"] and body not in out
    assert out.count("The rest of the document is about something else.") == 3


# --- what is shown about a finding ------------------------------------------------------------


def test_a_token_used_as_the_user_of_a_url_is_one_replacement_not_two():
    out, found = redaction.redact("https://" + POSITIVE["github-token"][1] + "@github.com/x/y.git")
    assert found == ["github-token"] and out == "https://[REDACTED:github-token]@github.com/x/y.git"


def test_names_that_describe_a_credential_are_not_credentials():
    for name in ("token_env", "api_key_header", "secret_name", "auth_sha", "password_hash", "key_id", "tokenUrl", "SECRET_FILE_PATH"):
        text = name + ' = "' + T("9f8a7b6c5d4e", "3f2a1b0c9d8e7f6a") + '"'
        assert redaction.redact(text) == (text, []), name


# --- a key that is indented, as YAML, heredocs and string literals hold it -------------------


@pytest.mark.parametrize("indent", ["", "  ", "    ", "        ", "\t", "\t\t  "])
def test_an_indented_private_key_is_replaced(indent):
    lines = ["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj",
             "MzEfYyjiWA4R4/M2bS1GB4t7NXp98C3SC6dVMvDuictGeurT8jNbvJZHtCSuYEvu",
             "NMoSfm76oqFvAp8Gy0iz5sxjZmSnXyCd"]
    text = ("tls:\n  key: |\n" + indent + T("-----BEGIN RSA ", "PRIVATE KEY-----") + "\n"
            + "".join(indent + line + "\n" for line in lines)
            + indent + T("-----END RSA ", "PRIVATE KEY-----") + "\n  cert: after\n")
    out, found = redaction.redact(text)
    assert found == ["private-key"]
    assert all(line not in out for line in lines)
    assert out.startswith("tls:\n  key: |\n") and out.endswith("  cert: after\n")


def test_a_private_key_with_windows_line_endings_is_replaced():
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj"
    text = T("-----BEGIN ", "PRIVATE KEY-----") + "\r\n" + body + "\r\n" + body + "\r\n" + T("-----END ", "PRIVATE KEY-----") + "\r\nafter"
    out, found = redaction.redact(text)
    assert found == ["private-key"] and body not in out and out.endswith("after")


# --- which part of a name says the value is about a credential ------------------------------


@pytest.mark.parametrize("name", ["ENV_API_KEY", "FILE_TOKEN", "INDEX_API_KEY", "LABEL_TOKEN", "X_API_KEY_HEADER_VALUE",
                                  "API_KEY_V2", "AUTH_TOKEN_PROD", "STRIPE_SECRET_KEY_LIVE"])
def test_a_word_before_the_keyword_does_not_excuse_the_value(name):
    """`ENV_API_KEY` is an API key. Only what comes AFTER the credential word
    says what the value is (`API_KEY_ENV` names a variable)."""
    value = T("Zk9f2Qm7Xw4Lp1", "Rt8Vb3Nc6Hs5Jd0")
    for text in (f'{name} = "{value}"', f"{name}={value}"):
        out, found = redaction.redact(text)
        assert value not in out and len(found) == 1, text


@pytest.mark.parametrize("name", ["EXAMPLE_API_KEY", "DUMMY_SECRET_KEY", "SAMPLE_TOKEN", "fake_password", "mockApiKey"])
def test_a_name_that_says_the_value_is_made_up_is_left_alone(name):
    text = f'{name} = "{T("Zk9f2Qm7Xw4Lp1", "Rt8Vb3Nc6Hs5Jd0")}"'
    assert redaction.redact(text) == (text, [])


def test_a_short_quoted_password_with_symbols_is_replaced():
    value = T("Xk9f!Q2m", "Z#pL7wRt")
    out, found = redaction.redact(f'password: "{value}"')
    assert found == ["credential-literal"] and value not in out
