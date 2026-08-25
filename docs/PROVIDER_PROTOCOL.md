# Provider Protocol

## Command contract

A `command` provider is an explicit argument array. Ariadne runs it without a shell, sends the role prompt to standard input, and captures standard output and standard error.

The process receives:

```text
ARIADNE_ROLE
ARIADNE_SLOT
ARIADNE_PROJECT_ROOT
ARIADNE_NETWORK_POLICY
ARIADNE_ROUTE_ID
ARIADNE_EPOCH
```

The process should return one raw JSON object:

```json
{ "...": "..." }
```

For backward compatibility, Ariadne also accepts an object wrapped between
`<ARIADNE_JSON>` and `</ARIADNE_JSON>` tags. Schema-enforcing providers such as
the packaged Codex integration should return raw JSON only. The exact schema is
included in each prompt.

## Usage reporting

A provider may add:

```json
{
  "usage": {
    "input_tokens": 1000,
    "output_tokens": 500,
    "cost_usd": 0.25
  }
}
```

If absent, Ariadne uses `estimated_cost_usd` from the provider configuration.

## Wrapper guidance

Use a small wrapper around any model CLI or local inference server. The wrapper should:

1. read all of standard input;
2. call the model with that prompt;
3. emit the model’s final structured response;
4. propagate failures through a nonzero exit status;
5. avoid returning hidden reasoning traces.

See `examples/wrappers/minimal_agent.py`.
