# Error Reference

When an engine fails, Untether scans the error message and shows an actionable recovery hint above the raw error. The raw error is wrapped in a code block for visual separation.

This page lists all recognised error patterns grouped by category. Hints are matched by substring (case-insensitive) — first match wins.

## Authentication

| Pattern | Hint | Engines |
|---------|------|---------|
| `access token could not be refreshed` | Run `codex login --device-auth` to re-authenticate. | Codex |
| `log out and sign in again` | Run `codex login` to re-authenticate. | Codex |
| `anthropic_api_key` | Check that ANTHROPIC_API_KEY is set in your environment. | Claude, Pi |
| `openai_api_key` | Check that OPENAI_API_KEY is set in your environment. | Codex, OpenCode |
| `google_api_key` | Check that your Google API key is set in your environment. | Gemini |
| `authentication_error` | API key is invalid or expired. Check your API key configuration. | Claude, Pi |
| `invalid_api_key` / `api_key_invalid` | API key is invalid or expired. Check your API key configuration. | All |
| `invalid x-api-key` | API key is invalid or expired. Check your API key configuration. | Claude |

## Subscription and billing

| Pattern | Hint | Engines |
|---------|------|---------|
| `out of extra usage` | Subscription usage limit reached — wait for the reset window, then resume. | Claude |
| `hit your limit` | Subscription usage limit reached — wait for the reset window, then resume. | Claude |
| `insufficient_quota` | OpenAI billing quota exceeded. Check platform.openai.com and add credits. | Codex, OpenCode |
| `exceeded your current quota` | OpenAI billing quota exceeded. Check platform.openai.com and add credits. | Codex, OpenCode |
| `billing_hard_limit_reached` | OpenAI billing hard limit reached. Increase your spend limit. | Codex, OpenCode |
| `resource_exhausted` | Google API quota exhausted. Check console.cloud.google.com. | Gemini |

## API overload and server errors

| Pattern | Hint | Engines |
|---------|------|---------|
| `overloaded_error` | Anthropic API is overloaded — temporary. Try again in a few minutes. | Claude |
| `server is overloaded` | The API server is overloaded — temporary. Try again in a few minutes. | All |
| `internal_server_error` | Internal server error — usually temporary. Try again shortly. | All |
| `bad gateway` | Bad gateway error (502) — usually temporary. Try again shortly. | All |
| `service unavailable` | API temporarily unavailable (503). Try again in a few minutes. | All |
| `gateway timeout` | API gateway timed out (504) — usually temporary. Try again shortly. | All |

## Rate limits

| Pattern | Hint | Engines |
|---------|------|---------|
| `rate limit` | Rate limited — the engine will retry automatically. | All |
| `too many requests` | Rate limited — the engine will retry automatically. | All |

## Model errors

| Pattern | Hint | Engines |
|---------|------|---------|
| `model_not_found` | Model not available. Check the model name in `/config`. | All |
| `invalid_model` | Model not available. Check the model name in `/config`. | All |
| `model not available` | Model not available. Check the model name in `/config`. | All |
| `does not exist` | The requested resource was not found. Check your model or configuration. | All |

## Context length

| Pattern | Hint | Engines |
|---------|------|---------|
| `context_length_exceeded` | Session context is too long. Start a fresh session with `/new`. | Claude, Codex, OpenCode |
| `max_tokens` | Token limit exceeded. Start a fresh session with `/new`. | Claude, Codex, OpenCode |
| `context window` | Session context is too long. Start a fresh session with `/new`. | Claude, Codex, OpenCode |
| `too many tokens` | Token limit exceeded. Start a fresh session with `/new`. | All |

## Content safety

| Pattern | Hint | Engines |
|---------|------|---------|
| `content_filter` | Request blocked by content safety filter. Try rephrasing your prompt. | Claude, Gemini |
| `harm_category` | Request blocked by content safety filter. Try rephrasing your prompt. | Gemini |
| `prompt_blocked` | Request blocked by content safety filter. Try rephrasing your prompt. | Gemini |
| `safety_block` | Request blocked by content safety filter. Try rephrasing your prompt. | Gemini |

## Invalid request

| Pattern | Hint | Engines |
|---------|------|---------|
| `invalid_request_error` | Invalid API request. Try updating the engine CLI to the latest version. | Claude, Codex |

## Session errors

| Pattern | Hint | Engines |
|---------|------|---------|
| `session not found` | Try a fresh session without --session flag. | All |

## Network and connection errors

| Pattern | Hint | Engines |
|---------|------|---------|
| `connection refused` | Check that the target service is running. | All |
| `connecttimeout` | Connection timed out. Check your network, then try again. | All |
| `readtimeout` | Connection timed out — usually transient. Try again. | All |
| `name or service not known` | DNS resolution failed — check your network connection. | All |
| `network is unreachable` | Network is unreachable — check your internet connection. | All |
| `certificate verify failed` | SSL certificate verification failed. Check network, proxy, or certificates. | All |
| `ssl handshake` | SSL/TLS handshake failed. Check network, proxy, or certificates. | All |

## CLI and filesystem errors

| Pattern | Hint | Engines |
|---------|------|---------|
| `command not found` | Engine CLI not found. Check that it is installed and in your PATH. | All |
| `enoent` | Engine CLI not found. Check that it is installed and in your PATH. | All |
| `no space left` | Disk full — free up space and try again. | All |
| `permission denied` | Permission denied — check file and directory permissions. | All |
| `read-only file system` | File system is read-only — check mount and permissions. | All |

## Signal errors

| Pattern | Hint | Engines |
|---------|------|---------|
| `sigterm` | Untether was restarted. Your session is saved — resume by sending a new message. | All |
| `sigkill` | The process was forcefully terminated (timeout or out of memory). Resume by sending a new message. | All |
| `sigabrt` | The process aborted unexpectedly. Try starting a fresh session with `/new`. | All |

## Process and execution errors

| Pattern | Hint | Engines |
|---------|------|---------|
| `error_during_execution` | The session could not be loaded. Send `/new` to start a fresh session. | Claude |
| `finished without a result event` | The engine exited before producing a final answer. Try sending a new message to resume. | All |
| `finished but no session_id` | The engine crashed during startup. Check that the CLI is installed and working. | All |

## Engine-specific errors

| Pattern | Hint | Engines |
|---------|------|---------|
| `require paid credits` | AMP execute mode requires paid credits. Add credits at ampcode.com/pay. | AMP |
| `amp login` | Run `amp login` to authenticate with Sourcegraph. | AMP |
| `gemini result status:` | Gemini returned an unexpected result. Try a fresh session with `/new`. | Gemini |

## Account errors

| Pattern | Hint | Engines |
|---------|------|---------|
| `account_suspended` | Your account has been suspended. Check your provider's dashboard. | All |
| `account_disabled` | Your account has been disabled. Check your provider's dashboard. | All |

## Proxy and timeout errors

| Pattern | Hint | Engines |
|---------|------|---------|
| `407 proxy` | Proxy authentication required. Check your proxy configuration. | All |
| `deadline exceeded` | Request timed out — usually transient. Try again. | All |
| `timeout exceeded` | Request timed out — usually transient. Try again. | All |

## Exit code errors

| Pattern | Hint | Engines |
|---------|------|---------|
| `rc=137` / `rc=-9` | Forcefully terminated (out of memory). Resume by sending a new message. | All |
| `rc=143` / `rc=-15` | Terminated by signal (SIGTERM). Resume by sending a new message. | All |
