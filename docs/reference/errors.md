# Error Reference

When an engine fails, Untether scans the error message and shows an actionable recovery hint above the raw error. The raw error is wrapped in a code block for visual separation.

This page lists all recognised error patterns grouped by category, in the order they are checked. Hints are matched by substring (case-insensitive) — first match wins, so the order is part of the behaviour.

## Engine end-of-life and CLI flag drift

These are checked first. The end-of-life patterns must outrank the generic `invalid_request_error` pattern, which AMP's `426` payload would otherwise match with a misleading hint ([#721](https://github.com/littlebearapps/untether/issues/721)); the Codex retired-setting pattern must outrank the generic `no longer supported` fallback, which would otherwise blame the client version ([#830](https://github.com/littlebearapps/untether/issues/830)).

| Pattern | Hint | Engines |
|---------|------|---------|
| `ineligibletiererror` / `gemini code assist for individuals` | Gemini CLI is end-of-life for individual and free Google accounts (18 June 2026). The `gemini` engine is deprecated in Untether — switch engines via /config, or migrate to Antigravity CLI (antigravity.google). | Gemini (deprecated) |
| `this version of amp is no longer supported` | AMP is refusing this client version. Run `amp update` to upgrade. The `amp` engine is deprecated in Untether and may stop working again without notice. | AMP (deprecated) |
| `is no longer supported; remove this setting` | A setting in your Codex config (`~/.codex/config.toml`, a `--profile` file, or `[engines.codex] extra_args`) is no longer supported by the installed Codex CLI — remove the key the error names. | Codex |
| `' for '--` / `a value is required for '--` / `error: unexpected argument '-` | The engine CLI rejected a command-line flag — this Untether version may not match the installed CLI. Update Untether, and report it if the problem persists. | Codex (clap argv errors) |
| `flags provided but not defined` | The engine CLI rejected a command-line flag — this Untether version may not match the installed CLI. Update Untether, and report it if the problem persists. | Antigravity (agy argv errors) |
| `no longer supported` | The engine CLI reports that this client version or account tier is no longer supported by its provider. Update the CLI, or switch engines via /config. | All |

## Authentication

| Pattern | Hint | Engines |
|---------|------|---------|
| `isn't signed in on this host` | On a server, the Gemini API key route (`modelProvider: "gemini"` + `GEMINI_API_KEY`) or Enterprise ADC (`AGY_ADC_AUTH=true`) avoids interactive sign-in. | Antigravity |
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
| `quota is used up` | /usage shows each quota group and when it resets. | Antigravity |
| `ai credits balance is too low` | /usage shows each quota group and when it resets. | Antigravity |

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

Every Antigravity model or effort rejection starts with `invalid model selection`, so the effort patterns are checked before it.

| Pattern | Hint | Engines |
|---------|------|---------|
| `" effort (available:` / `invalid --effort` / `conflicts with --effort` | That effort level isn't available for this Antigravity model — pick another in /config → Effort, or use a model id without an effort suffix. | Antigravity |
| `invalid model selection` | Antigravity doesn't recognise that model. Run `agy models` on the host for the list, or `/model clear`. | Antigravity |
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

## Reasoning level

These are checked before the generic `invalid_request_error` pattern, because the same 400 carries that type too. The hints are engine-neutral: `reasoning.effort` is an OpenAI Responses-API parameter, so an OpenCode run on an OpenAI provider can hit it as well.

| Pattern | Hint | Engines |
|---------|------|---------|
| `cannot be used with reasoning.effort` | The model's reasoning level can't be combined with a tool that is switched on (usually web search). Raise the reasoning level: in /config → Reasoning if this engine offers it there, otherwise in the engine's own config file (for Codex, model_reasoning_effort in ~/.codex/config.toml). | Codex, OpenCode (OpenAI) |
| `reasoning.effort` | The model doesn't accept this reasoning level. Choose another one: in /config → Reasoning if this engine offers it there, otherwise in the engine's own config file (for Codex, model_reasoning_effort in ~/.codex/config.toml). | Codex, OpenCode (OpenAI) |

## Invalid request

| Pattern | Hint | Engines |
|---------|------|---------|
| `invalid_request_error` | The API rejected the request (invalid_request_error) — the error below says why. Check the model and reasoning settings in /config; if they look right, update the engine CLI. | Claude, Codex |

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
| `require paid credits` | AMP execute mode requires paid credits. Add credits at ampcode.com/pay. | AMP (deprecated) |
| `amp login` | Run `amp login` to authenticate with Sourcegraph. | AMP (deprecated) |
| `antigravity ended with status canceled` | Antigravity cancelled the turn upstream (long turns sometimes do, antigravity-cli#902). Retry, or split the task into smaller steps. | Antigravity |
| `gemini result status:` | Gemini returned an unexpected result. Try a fresh session with `/new`. | Gemini (deprecated) |

## Antigravity messages

Antigravity CLI (`agy`) errors are Untether's own messages, written when the runner recognises what went wrong. Where a hint above applies, it is shown with them. The agy output that triggers each one is listed in the [Antigravity runner reference](runners/antigravity/runner.md#errors).

| Message starts | Meaning | What to do |
|---|---|---|
| `Antigravity CLI isn't signed in on this host` | agy wanted a browser sign-in | Sign in with `agy` in a terminal on the host, or use the API-key or Enterprise route |
| `Google is asking this account to verify itself or appeal a Terms of Service block` | Google has blocked or challenged the account | Run `agy` in a terminal on the host to see Google's link. Untether won't retry |
| `This Antigravity quota is used up` | A quota group is exhausted | `/usage` shows when each group resets |
| `Antigravity's AI credits balance is too low to continue` | Out of AI credits | Top up, or wait for the quota reset |
| `That Antigravity conversation no longer exists` | agy doesn't know the resume id | Send the message again; a new conversation starts |
| `Antigravity was interrupted` | The run was cancelled or the process was signalled | Reply to the resume line to continue |
| `Antigravity needs a project` | The chat has no project directory | `/ctx set <project>`, or add a `[projects.*]` entry |
| `🛑 Antigravity CLI <version> is older than 1.3.1` | agy is too old | `agy update` on the host |
| `Ask me needs Untether's approval gate` / `Plan first needs …` | That mode isn't available yet | Pick Workspace or Full access in `/config` |
| `agy's own settings change its permission checks` | agy's `toolPermission` setting would approve tools by itself | Set it back to `request-review`, or pick Full access |
| `agy's workspace config changed since someone last ran Antigravity` | A scheduled run was held because the project's agy hooks, plugins or MCP config changed | Send any message in the chat, review the ⚠️ row; the schedule then runs again |
| `Untether couldn't check agy's hooks, plugins and MCP servers` | A scheduled run was held because the check couldn't finish | Send a message in the chat to see why, or trim the project's `.agents/` folder |
| `agy reported a model/agent error` | agy exited with a turn-level failure | Retry; check `/usage` |
| `antigravity ended with status <status>` | agy finished with a status other than success or error | Retry, or split the task |
| `antigravity failed (<exit>)` | agy exited without a result | Read the excerpt underneath; URLs, tokens and paths in it are redacted |

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
