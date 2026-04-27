# Reddit dad-joke scraper

Small local service that fetches `r/fadervittigheder` once a day using Reddit's `.json` listing URL.

It stores posts in SQLite and exposes:

- `http://localhost:18080/` - simple local page
- `http://localhost:18080/api/posts` - stored posts as JSON
- `http://localhost:18080/api/status` - scraper status
- `http://localhost:18080/api/refresh` - trigger a background refresh

## Run with Docker Compose

```powershell
docker compose up -d --build
```

Docker Compose publishes the service on `http://localhost:18080/` by default.
Change it with `HOST_PORT`, for example:

```powershell
$env:HOST_PORT="18081"
docker compose up -d
```

The SQLite database is stored in the Docker volume `dadabase_reddit-data`.

## Run without Docker

```powershell
python app.py
```

Without Docker, the default URL is `http://localhost:8080/`.

## Configuration

Environment variables:

- `SUBREDDIT` - defaults to `fadervittigheder`
- `FETCH_LIMIT` - defaults to `100`
- `FETCH_INTERVAL_SECONDS` - defaults to `86400`
- `PORT` - defaults to `8080`
- `DATA_DIR` - defaults to `data`
- `USER_AGENT` - custom Reddit User-Agent

The service follows the Reddit JSON trick described by Simon Willison: add `.json` to a Reddit URL and send a custom User-Agent.
