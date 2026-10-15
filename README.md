# CamAgent

## Interactive CLI

Run the module manager prompt from the project root:

```bash
python -m app.cli
```

Enter `help` to see the supported commands. The CLI accepts `list`, `info <module>`,
`loaded <module>`, `load <module>`, `unload <module>`, and `reload <module>`.
These requests can also be entered as JSON, for example:

```json
{"command": "info", "module": "uvcvideo"}
```

Blank input is ignored. Enter `exit` or `quit` to leave the prompt; end-of-input
also exits cleanly.