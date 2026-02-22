from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import httpx
from datetime import datetime, timedelta
import os
import asyncio

app = FastAPI(title="GitHub Repository Analyzer")

# Setup templates
templates = Jinja2Templates(directory="templates")

# Static files for caching
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

GITHUB_API_BASE = "https://api.github.com"

class GitHubAPIError(Exception):
    pass

class RateLimitError(GitHubAPIError):
    pass

class RepoNotFoundError(GitHubAPIError):
    pass

async def get_repo_data(owner: str, repo: str):
    """Fetch comprehensive repo data from GitHub API"""
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        headers = {"Accept": "application/vnd.github.v3+json"}
        
        # Basic repo info
        repo_resp = await client.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}",
            headers=headers
        )
        
        if repo_resp.status_code == 404:
            raise RepoNotFoundError(f"Repository {owner}/{repo} not found")
        elif repo_resp.status_code == 403:
            raise RateLimitError("GitHub API rate limit exceeded. Try again later.")
        elif repo_resp.status_code != 200:
            raise GitHubAPIError(f"GitHub API error: {repo_resp.status_code}")
            
        repo_data = repo_resp.json()
        
        # Parallel requests for better performance
        lang_task = client.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/languages",
            headers=headers
        )
        contrib_task = client.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contributors?per_page=10",
            headers=headers
        )
        commit_task = client.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/stats/commit_activity",
            headers=headers
        )
        releases_task = client.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases?per_page=5",
            headers=headers
        )
        
        # Execute all requests
        lang_resp, contrib_resp, commit_resp, releases_resp = await asyncio.gather(
            lang_task, contrib_task, commit_task, releases_task,
            return_exceptions=True
        )
        
        # Process responses
        languages = lang_resp.json() if not isinstance(lang_resp, Exception) and lang_resp.status_code == 200 else {}
        contributors = contrib_resp.json() if not isinstance(contrib_resp, Exception) and contrib_resp.status_code == 200 else []
        commit_activity = commit_resp.json() if not isinstance(commit_resp, Exception) and commit_resp.status_code == 200 else []
        releases = releases_resp.json() if not isinstance(releases_resp, Exception) and releases_resp.status_code == 200 else []
        
        return {
            "repo": repo_data,
            "languages": languages,
            "contributors": contributors,
            "commit_activity": commit_activity,
            "releases": releases,
            "fetched_at": datetime.now().isoformat()
        }

@app.exception_handler(RepoNotFoundError)
async def not_found_handler(request: Request, exc: RepoNotFoundError):
    if request.headers.get("accept") == "application/json":
        return JSONResponse(
            status_code=404,
            content={"error": "Repository not found", "detail": str(exc)}
        )
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "error": "Repository not found", "detail": str(exc)},
        status_code=404
    )

@app.exception_handler(RateLimitError)
async def rate_limit_handler(request: Request, exc: RateLimitError):
    if request.headers.get("accept") == "application/json":
        return JSONResponse(
            status_code=429,
            content={"error": "Rate limit exceeded", "detail": str(exc)}
        )
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "error": "Rate limit exceeded", "detail": str(exc)},
        status_code=429
    )

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Landing page with input form"""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/analyze/{owner}/{repo}", response_class=HTMLResponse)
async def analyze_repo(request: Request, owner: str, repo: str):
    """Full analysis dashboard"""
    try:
        data = await get_repo_data(owner, repo)
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "data": data, "owner": owner, "repo": repo}
        )
    except (RepoNotFoundError, RateLimitError):
        raise
    except Exception as e:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "error": "Analysis failed", "detail": str(e)},
            status_code=500
        )

@app.get("/api/{owner}/{repo}")
async def api_analyze(owner: str, repo: str):
    """JSON API endpoint"""
    try:
        data = await get_repo_data(owner, repo)
        return data
    except RepoNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RateLimitError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/compare", response_class=HTMLResponse)
async def compare_repos_form(request: Request):
    """Comparison input form"""
    return templates.TemplateResponse("compare.html", {"request": request})

@app.get("/compare/{repos:path}", response_class=HTMLResponse)
async def compare_repos(request: Request, repos: str):
    """Compare multiple repositories"""
    try:
        # Parse repos from URL (format: owner1/repo1/owner2/repo2/...)
        parts = repos.strip('/').split('/')
        if len(parts) < 4 or len(parts) % 2 != 0:
            return templates.TemplateResponse(
                "error.html",
                {"request": request, "error": "Invalid format", "detail": "Use format: owner1/repo1/owner2/repo2"},
                status_code=400
            )
        
        # Build repo list
        repo_list = []
        for i in range(0, len(parts), 2):
            owner, repo = parts[i], parts[i+1]
            repo_list.append((owner, repo))
        
        # Fetch all repos in parallel
        tasks = [get_repo_data(owner, repo) for owner, repo in repo_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        repos_data = []
        for i, result in enumerate(results):
            owner, repo = repo_list[i]
            if isinstance(result, Exception):
                repos_data.append({
                    "owner": owner,
                    "repo": repo,
                    "error": str(result),
                    "data": None
                })
            else:
                repos_data.append({
                    "owner": owner,
                    "repo": repo,
                    "error": None,
                    "data": result
                })
        
        return templates.TemplateResponse(
            "comparison.html",
            {"request": request, "repos": repos_data}
        )
    except Exception as e:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "error": "Comparison failed", "detail": str(e)},
            status_code=500
        )

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0"
    }

if __name__ == "__main__":
    import uvicorn
    import asyncio
    uvicorn.run(app, host="0.0.0.0", port=8000)
