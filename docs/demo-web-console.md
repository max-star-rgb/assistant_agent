# Demo Web Console

The minimal local web console is served by FastAPI:

```text
GET /demo/console
```

It uses these local endpoints:

```text
GET /demo/scenarios
POST /agent/run
GET /runs/{run_id}
GET /traces/{trace_id}
```

## Run Locally

Start the app with your normal FastAPI server command, then open:

```text
http://127.0.0.1:8000/demo/console
```

The console supports:

- text input
- demo scenario selection
- optional `image_ref`
- optional `video_ref`
- response text display
- tool call display
- run id and trace id display
- error display

## Safety Boundary

- No login or production permissions are implemented.
- No real external Provider is called by default.
- Default execution remains mock/local/offline.
- Image and video refs are logical mock/local ids, not real uploaded files.
