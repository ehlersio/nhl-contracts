"""The thin loader: release URLs, caching, formats (network mocked)."""

import importlib
import json

import httpx

import nhl_contracts

loader = importlib.import_module("nhl_contracts.load")  # the package exports load() the function


def test_public_api_and_urls():
    assert nhl_contracts.load is loader.load and nhl_contracts.__version__
    assert loader.file_url("contracts.json") == (
        "https://github.com/ehlersio/nhl-contracts/releases/download/contracts/contracts.json")


def test_load_json_csv_and_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("NHL_CONTRACTS_CACHE", str(tmp_path))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("contracts.json"):
            return httpx.Response(200, json=[{"player_name": "Marat Khusnutdinov", "aav_usd": 3_950_000}])
        if request.url.path.endswith("sources.csv"):
            return httpx.Response(200, text='source_id,url\nforge:a,"https://x,y"\n')
        if request.url.path.endswith("timestamp.json"):
            return httpx.Response(200, json={"n_contracts": 6961})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(loader.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    rows = loader.load("contracts")
    assert rows[0]["aav_usd"] == 3_950_000
    assert loader.load("contracts")[0]["player_name"] == "Marat Khusnutdinov"  # served from cache
    assert calls.count("/ehlersio/nhl-contracts/releases/download/contracts/contracts.json") == 1
    assert loader.load("sources") == [{"source_id": "forge:a", "url": "https://x,y"}]
    assert loader.timestamp()["n_contracts"] == 6961
    assert json.loads((tmp_path / "timestamp.json").read_text())["n_contracts"] == 6961
    try:
        loader.load("nope")
    except ValueError as exc:
        assert "unknown table" in str(exc)
    else:
        raise AssertionError("unknown table accepted")
