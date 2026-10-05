"""Read-only release check: remote main must match the expected SHA and its CI.

Credentials are optional for public repositories; only safe status data is printed.
No workflow dispatch, push, merge or retry of business writes is performed here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx


async def check(args: argparse.Namespace) -> int:
    headers = {'Accept':'application/vnd.github+json', 'X-GitHub-Api-Version':'2022-11-28'}
    if args.credentials:
        credentials = json.loads(Path(args.credentials).read_text(encoding='utf-8'))
        headers['Authorization'] = 'Bearer '+credentials['EVIFORGE_GITHUB_TOKEN']
    report = {'repository':args.repository, 'expected_sha':args.expected_sha, 'status':'pending'}
    try:
        async with httpx.AsyncClient(base_url='https://api.github.com', headers=headers, follow_redirects=False, timeout=30) as client:
            async def get(path, params=None):
                response = await client.get(path, params=params)
                if response.status_code != 200:
                    raise RuntimeError('GitHub API HTTP '+str(response.status_code))
                return response.json()
            prefix = '/repos/'+args.repository
            ref = await get(prefix+'/git/ref/heads/main')
            report['remote_main_sha'] = ref['object']['sha']
            if report['remote_main_sha'] != args.expected_sha:
                report['status'] = 'main_mismatch'
            else:
                runs = await get(prefix+'/actions/workflows/ci.yml/runs', {'head_sha':args.expected_sha,'branch':'main','event':'push','per_page':10})
                found = runs['workflow_runs']
                if found:
                    run = found[0]
                    jobs = await get(prefix+'/actions/runs/'+str(run['id'])+'/jobs', {'per_page':100})
                    report.update(ci_url=run['html_url'], ci_run_id=run['id'], ci_status=run['status'], ci_conclusion=run['conclusion'],
                                  jobs=[{'name':job['name'],'status':job['status'],'conclusion':job['conclusion']} for job in jobs['jobs']])
                    if run['status'] == 'completed':
                        report['status'] = 'passed' if run['conclusion'] == 'success' and report['jobs'] and all(job['conclusion']=='success' for job in report['jobs']) else 'ci_failed'
    except Exception as exc:
        report.update(status='check_failed', error_type=type(exc).__name__)
    if args.output:
        target = Path(args.output); target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    return 0 if report['status']=='passed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default='KNIGHT15235/EviForge')
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--credentials', help='Optional explicitly selected local ignored JSON')
    parser.add_argument('--output')
    raise SystemExit(asyncio.run(check(parser.parse_args())))
