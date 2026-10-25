"""Live retrieval checks against supplied books; run after ingestion."""
import asyncio
import json
import re
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE='http://localhost:8000'
CASES=[
    ('Players','keeping a spell active after getting hurt',r'Constitution\s+saving\s+throw\s+to\s+maintain'),
    ('Players','how to recover lost health while sleeping',r'long rest|short rest|hit point'),
    ('Monster','a creature whose stare turns victims into stone',r'basilisk|medusa|petrif'),
    ('Monster','a floating monster with many magical eye beams',r'beholder|eye ray'),
]


async def main():
    report=[]
    async with httpx.AsyncClient(timeout=300) as client:
        books=(await client.get(BASE+'/books')).json()
        assert len(books)>=2,books
        index=(await client.get(BASE+'/index')).json()
        assert index['ready'] and index['qdrant_points']==index['sqlite_chunks']
        assert index['indexed_vectors']>0
        print('Qdrant index:',json.dumps(index))
        for name,query,expected in CASES:
            book=next(b for b in books if name.lower() in b['filename'].lower())
            r=await client.post(BASE+'/search',json={'query':query,'book_id':book['id'],'token_budget':1200})
            r.raise_for_status()
            data=r.json()
            assert data['retrieval_backend']=='qdrant'
            passed=bool(re.search(expected,' '.join(x['text'] for x in data['results']),re.I))
            assert data['text_tokens']<=1200
            report.append({'query':query,'passed':passed,'pages':[x['page'] for x in data['results']],
                           'tokens':data['text_tokens']})
        first=books[0]
        original=await client.get(BASE+f"/books/{first['id']}/pdf",headers={'Range':'bytes=0-1023'})
        assert original.status_code==206 and original.content.startswith(b'%PDF')
        assert len(original.content)==1024
        page=await client.get(BASE+f"/books/{first['id']}/pages/10?token_budget=128")
        assert page.status_code==200 and page.json()['text_tokens']<=128
        monster=next(b for b in books if b['filename']=='Monster Manual.pdf')
        repaired=(await client.get(BASE+f"/books/{monster['id']}/pages/20?token_budget=8000")).json()['text']
        assert 'ANIMATED FLYING SWORD' in repaired.upper()
        assert 'ANIMATED RUG OF SMOTHERING' in repaired.upper()
        assert re.search(r'\bHP\s+14\b',repaired) and re.search(r'\bHP\s+27\b',repaired)
        assert sum(len(line.strip())<=2 for line in repaired.splitlines() if line.strip())<5
        listing=(await client.get(BASE+f"/books/{monster['id']}/pages/382?token_budget=8000")).json()['text']
        assert listing.index('Aboleth')<listing.index('Brown Bear')<listing.index('Mastiff')<listing.index('Animated Armor')
        print('Reported PDF pages 20 and 382: repaired text and column order verified through REST.')
        async with streamablehttp_client(BASE+'/mcp/') as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize()
                names=[t.name for t in (await session.list_tools()).tools]
                assert set(names)=={'list_books','search_books','read_chunk','read_page'}
                result=await session.call_tool('search_books',{'query':'maintaining concentration','token_budget':300})
                assert not result.isError
                payload=json.loads(result.content[0].text)
                assert payload['retrieval_backend']=='qdrant'
                assert payload['results'] and payload['text_tokens']<=300
                cid=payload['results'][0]['id']
                result=await session.call_tool('read_chunk',{'chunk_id':cid})
                assert not result.isError
                print('MCP initialized; four tools discovered; search + read verified.')
    print(json.dumps(report,indent=2))
    assert all(r['passed'] for r in report),'Some retrieval quality checks failed'


if __name__=='__main__':
    asyncio.run(main())
