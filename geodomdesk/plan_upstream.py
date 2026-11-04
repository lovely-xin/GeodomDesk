#!/usr/bin/env python3
"""Resolve the latest official release and decide whether a custom rebuild is needed."""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from channel import version_tuple


def get(path):
    headers={'User-Agent':'GeodomDesk-Upstream-Sync','Accept':'application/vnd.github+json'}
    if os.environ.get('GITHUB_TOKEN'):
        headers['Authorization']='Bearer '+os.environ['GITHUB_TOKEN']
    with urllib.request.urlopen(urllib.request.Request('https://api.github.com'+path,headers=headers),timeout=40) as response:
        raw=response.read(2*1024*1024+1)
        if len(raw)>2*1024*1024:
            raise ValueError('GitHub response too large')
        return json.loads(raw)


def plan():
    release=get('/repos/rustdesk/rustdesk/releases/latest')
    tag=release['tag_name']
    version=tag.removeprefix('v')
    version_tuple(version)
    if release['draft'] or release['prerelease']:
        raise ValueError('Expected the latest stable official release')
    upstream=get('/repos/rustdesk/rustdesk/commits/'+urllib.parse.quote(tag,safe=''))['sha']
    if not re.fullmatch('[0-9a-f]{40}',upstream):
        raise ValueError('Invalid upstream source commit')
    repository=os.environ['GITHUB_REPOSITORY']
    required=True
    if os.environ.get('GITHUB_EVENT_NAME')=='schedule':
        try:
            own=get('/repos/'+repository+'/releases/latest')
            asset=next(a for a in own['assets'] if a['name']=='geodomdesk-build.json')
            with urllib.request.urlopen(urllib.request.Request(asset['browser_download_url'],headers={'User-Agent':'GeodomDesk-Upstream-Sync'}),timeout=30) as response:
                current=json.load(response)
            required=current.get('upstream_commit')!=upstream or current.get('customization_commit')!=os.environ['GITHUB_SHA']
        except (urllib.error.HTTPError,StopIteration,ValueError):
            required=True
    build_number=int(os.environ['GITHUB_RUN_NUMBER'])*100+int(os.environ.get('GITHUB_RUN_ATTEMPT','1'))
    result={'version':version,'official_tag':tag,'upstream_commit':upstream,
            'build_number':str(build_number),'build':'true' if required else 'false'}
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        for key,value in result.items():
            output.write(key+'='+value+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    plan()
