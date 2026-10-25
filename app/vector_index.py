"""Qdrant REST adapter. SQLite owns text and recoverable embeddings."""
import os
import uuid

import httpx
import numpy as np

URL=os.getenv('QDRANT_URL','http://qdrant:6333').rstrip('/')


class IndexUnavailable(RuntimeError):
    pass


def request(method,path,body=None,allow_missing=False):
    try:
        with httpx.Client(base_url=URL,timeout=httpx.Timeout(20,connect=2),trust_env=False) as client:
            response=client.request(method,path,json=body)
        if allow_missing and response.status_code==404:
            return None
        response.raise_for_status()
        return response.json()['result']
    except (httpx.HTTPError,KeyError,ValueError) as exc:
        raise IndexUnavailable('Qdrant semantic index unavailable; saved books remain intact. Retry or synchronize the index after recovery.') from exc


def point_id(chunk_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'local-book-library:'+chunk_id))


def ensure(collection,dimension):
    info=request('GET',f'/collections/{collection}',allow_missing=True)
    if info is not None:
        config=info['config']['params']['vectors']
        if config['size']!=dimension or config['distance']!='Cosine':
            raise IndexUnavailable('Vector index configuration differs from saved embeddings; rebuild the index.')
        return False
    request('PUT',f'/collections/{collection}',{
        'vectors':{'size':dimension,'distance':'Cosine'},
        'hnsw_config':{'m':16,'ef_construct':128,'full_scan_threshold':1000},
        'optimizers_config':{'default_segment_number':1,'indexing_threshold':1000,'max_optimization_threads':2}})
    for field in ('book_id','generation'):
        request('PUT',f'/collections/{collection}/index?wait=true',{'field_name':field,'field_schema':'keyword'})
    return True


def replace_book(collection,book_id,generation,rows):
    for start in range(0,len(rows),128):
        points=[{'id':point_id(r['id']),
                 'vector':np.frombuffer(r['vector'],dtype=np.float32).tolist(),
                 'payload':{'chunk_id':r['id'],'book_id':book_id,'generation':generation}}
                for r in rows[start:start+128]]
        request('PUT',f'/collections/{collection}/points?wait=true',{'points':points})
    # Delete only obsolete generations after the current generation was acknowledged.
    request('POST',f'/collections/{collection}/points/delete?wait=true',{'filter':{
        'must':[{'key':'book_id','match':{'value':book_id}}],
        'must_not':[{'key':'generation','match':{'value':generation}}]}})


def prune(collection,book_ids):
    # This collection is private to one SQLite library UUID and embedding model.
    condition={'must_not':[{'key':'book_id','match':{'any':book_ids}}]} if book_ids else {}
    request('POST',f'/collections/{collection}/points/delete?wait=true',{'filter':condition})


def query(collection,vector,book_id=None,limit=100):
    body={'query':vector.tolist(),'limit':limit,'with_payload':True,'with_vector':False,
          'params':{'hnsw_ef':128,'exact':False}}
    if book_id:
        body['filter']={'must':[{'key':'book_id','match':{'value':book_id}}]}
    result=request('POST',f'/collections/{collection}/points/query',body)
    return [p['payload']['chunk_id'] for p in result['points']]


def info(collection):
    return request('GET',f'/collections/{collection}',allow_missing=True)


def delete_collection(collection):
    return request('DELETE',f'/collections/{collection}',allow_missing=True)
