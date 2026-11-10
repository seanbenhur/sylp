# Providers

Sylp connects to model providers directly. Credentials live in `~/.sylp/auth.json` (owner-only permissions); every provider also accepts environment variables instead.

| Provider | Sign-in | Environment variables |
| --- | --- | --- |
| OpenAI (ChatGPT or API key) | `sylp auth login openai` / `sylp auth set openai` | `OPENAI_API_KEY` |
| Anthropic | `sylp auth set anthropic` | `ANTHROPIC_API_KEY` |
| Gemini | `sylp auth set gemini` | `GEMINI_API_KEY`, `GOOGLE_API_KEY` |
| Antigravity | `sylp auth login antigravity` / `sylp auth set antigravity` | `ANTIGRAVITY_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY` |
| OpenRouter | `sylp auth set openrouter` | `OPENROUTER_API_KEY` |
| OpenAI-compatible | `sylp auth set openai-compatible --base-url ...` | `SYLP_API_KEY`, `SYLP_BASE_URL` |

`sylp auth status` shows what is connected, `sylp models --provider <name>` lists models, and `sylp auth logout <name>` removes a credential. When several providers are connected, an interactive session asks which to use and remembers the choice; override with `SYLP_PROVIDER` and `SYLP_MODEL`.

## OpenAI

Two paths. `sylp auth login openai` signs in with a ChatGPT account in the browser and uses the plan's available usage. `sylp auth set openai` (or `OPENAI_API_KEY`) uses the OpenAI API billed to that key. Requests go to the Responses API with streaming.

## Anthropic

API key only, via the Messages API with streaming. Claude.ai subscriptions do not include Console API usage.

## Gemini

`sylp auth set gemini` saves a Gemini API key (create one at [Google AI Studio](https://aistudio.google.com/); no card required). Model listing merges the live `/models` listing with a curated fallback when the key is unavailable.

## Antigravity

Antigravity has two distinct credential types, and they are not interchangeable:

- **OAuth sign-in** (`sylp auth login antigravity`) opens Google's consent flow in a browser. If the Antigravity IDE is already signed in on the machine, Sylp reuses that session instead. OAuth access tokens (`ya29.*`) are issued for Google's **Code Assist backend**, not the Gemini Developer API; sending them to `generativelanguage.googleapis.com` returns 401. Sylp therefore routes OAuth requests to the Code Assist endpoints, onboarding the account (`loadCodeAssist` / `onboardUser`) to discover its Cloud project before generating. The project is cached for the session. This path does not stream: the response prints when the turn completes, and the model list is the curated Antigravity set because OAuth tokens cannot list models on the Developer API.
- **API key** (`sylp auth set antigravity`, `ANTIGRAVITY_API_KEY`, `GEMINI_API_KEY`, or `GOOGLE_API_KEY`) uses the OpenAI-compatible Gemini Developer API endpoint with streaming, the same as the Gemini provider. An Antigravity OAuth account can also fall back to a saved Gemini key.

## OpenRouter and compatible endpoints

OpenRouter requests identify Sylp via the `HTTP-Referer` and `X-Title` headers and include usage in the stream. An OpenAI-compatible provider needs only a base URL (and a key when the server requires one), which covers local model servers and proxies that speak the chat-completions API.

## Timeouts

Provider requests allow up to 15 minutes per response by default. Set `SYLP_REQUEST_TIMEOUT` to a value between 60 and 3600 seconds to change it.
