"""Real PostgreSQL exercises result activation and lease fencing."""
import os
import asyncio
import hashlib
from pathlib import Path
import tempfile
import unittest
import uuid

import psycopg
import httpx2
from psycopg import sql
from starlette.applications import Starlette
from vooy_storage.repository import Repository
from vooy_storage.rag import Conflict, RAGService, worker_routes


@unittest.skipUnless(os.getenv('VOOY_TEST_DATABASE_URL'), 'requires test PostgreSQL')
class RagIntegration(unittest.TestCase):
    def setUp(self):
        self.db = os.environ['VOOY_TEST_DATABASE_URL']
        self.schema = 'ragtest_' + uuid.uuid4().hex
        with psycopg.connect(self.db, autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(self.drop_schema)
        schema = self.schema
        class TestRepo(Repository):
            def connect(self):
                conn = super().connect()
                conn.execute(sql.SQL('SET search_path TO {}, public').format(sql.Identifier(schema)))
                return conn
        self.repo = TestRepo(self.db, Path(directory.name))
        self.repo.init_schema()
        self.rag = RAGService(self.repo)
        self.rag.init_schema()
        self.asset = self.repo.save_text('바다', '해변에서 산책')
        self.space = self.rag.register_space('test', '1', 3, 'cosine', 'unit', 'p1', ['text'],
                                             document_encoder='doc', query_encoder='query')

    def drop_schema(self):
        with psycopg.connect(self.db, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def job(self):
        job = self.rag.enqueue(self.asset['id'], self.space['id'], request_key=str(uuid.uuid4()))
        claimed = self.rag.claim(['v1'], [self.space['id']])
        self.assertEqual(job['id'], claimed['id'])
        return claimed

    def test_gemini_media_expansion_preserves_space_identity(self):
        original = self.rag.register_space('google/gemini-embedding-2', 'rev', 3, 'cosine', 'unit', 'p1',
                                           ['image', 'text'], document_encoder='doc', query_encoder='query')
        updated = self.rag.extend_gemini_modalities(original['id'])
        self.assertEqual(updated['id'], original['id'])
        self.assertEqual(updated['modalities'], ['audio', 'document', 'image', 'text', 'video'])
        repeated = self.rag.register_space('google/gemini-embedding-2', 'rev', 3, 'cosine', 'unit', 'p1',
                                           updated['modalities'], document_encoder='doc', query_encoder='query')
        self.assertEqual(repeated['id'], original['id'])
        with self.assertRaises(ValueError):
            self.rag.extend_gemini_modalities(self.space['id'])

    def batch(self, result, job, values=None):
        segments = [{'external_id':'a', 'kind':'text', 'text':'해변에서 산책', 'locator':{'char_start':0,'char_end':7},
                     'vectors':[{'ordinal':0,'values':values or [1,0,0]}]}]
        return self.rag.submit_batch(result['id'], 'batch-1', job['lease_epoch'], self.asset['version_id'], self.space['id'], segments)

    def test_activation_search_retry_and_deletion(self):
        job = self.job()
        result = self.rag.create_result(job['id'], job['lease_epoch'])
        one = self.batch(result,job)
        self.assertEqual(one['payload_hash'],self.batch(result,job)['payload_hash'])
        with self.assertRaises(Conflict):
            self.rag.complete(result['id'],job['lease_epoch'],{'batches':['batch-1'],'segment_count':2,'vector_count':1})
        manifest = {'batches':['batch-1'],'segment_count':1,'vector_count':1}
        self.assertEqual(self.rag.complete(result['id'],job['lease_epoch'],manifest)['state'],'active')
        self.assertEqual(self.rag.complete(result['id'],job['lease_epoch'],manifest)['state'],'active')
        self.assertEqual(self.rag.search_vector(self.space['id'],[1,0,0])['results'][0]['asset_id'],self.asset['id'])
        trip = self.repo.create_collection('여행')
        self.assertEqual(self.rag.search_vector(self.space['id'],[1,0,0],collection_id=trip['id'])['results'], [])
        self.repo.add_to_collection(trip['id'],self.asset['id'])
        self.assertEqual(self.rag.search_vector(self.space['id'],[1,0,0],collection_id=trip['id'])['results'][0]['asset_id'],self.asset['id'])
        new = self.job()
        self.assertEqual(len(self.rag.search_vector(self.space['id'],[1,0,0])['results']),1)
        result2 = self.rag.create_result(new['id'],new['lease_epoch'])
        self.batch(result2,new)
        self.repo.delete_asset(self.asset['id'])
        with self.assertRaises(Conflict):
            self.rag.complete(result2['id'],new['lease_epoch'],manifest)
        self.assertEqual(self.rag.search_vector(self.space['id'],[1,0,0])['results'],[])

    def test_expired_lease_prevents_stale_submit(self):
        first = self.job()
        result = self.rag.create_result(first['id'],first['lease_epoch'])
        with self.repo.connect() as conn:
            conn.execute("UPDATE rag_jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (first['id'],))
        second = self.rag.claim(['v1'], [self.space['id']])
        self.assertGreater(second['lease_epoch'],first['lease_epoch'])
        with self.assertRaises(Conflict):
            self.batch(result,first)
        self.assertEqual(self.rag.create_result(second['id'],second['lease_epoch'])['state'],'staging')

    def test_reject_wrong_dimension(self):
        job = self.job()
        result = self.rag.create_result(job['id'],job['lease_epoch'])
        with self.assertRaises(ValueError):
            self.batch(result,job,[1,0])

    def test_image_metadata_and_local_date_filter(self):
        data = b'example image bytes'
        path = self.repo.media_root / 'example.jpg'
        path.write_bytes(data)
        self.asset = self.repo.save_file('여행', 'example.jpg', 'image/jpeg', path, len(data), hashlib.sha256(data).hexdigest())
        self.space = self.rag.register_space('image-test', '1', 3, 'cosine', 'unit', 'p1', ['image'],
                                             document_encoder='doc', query_encoder='query')
        job = self.job()
        saved = self.rag.record_image_metadata(job['id'], job['lease_epoch'], '2025-05-10T16:30:00', 540, 33.5, 126.5)
        self.assertEqual(saved['captured_at_local'], '2025-05-10T16:30:00')
        self.assertEqual(self.rag.image_metadata(self.asset['id'], self.asset['version_id'])['latitude'], 33.5)
        result = self.rag.create_result(job['id'], job['lease_epoch'])
        segments = [{'external_id':'full-image', 'kind':'image', 'locator':{'bbox':[0,0,1,1]},
                     'vectors':[{'ordinal':0,'values':[1,0,0]}]}]
        self.rag.submit_batch(result['id'],'image-1',job['lease_epoch'],self.asset['version_id'],self.space['id'],segments)
        self.rag.complete(result['id'],job['lease_epoch'],{'batches':['image-1'],'segment_count':1,'vector_count':1})
        self.assertEqual(len(self.rag.search_vector(self.space['id'],[1,0,0],captured_from='2025-05-10',captured_to='2025-05-10')['results']),1)
        self.assertEqual(self.rag.search_vector(self.space['id'],[1,0,0],captured_to='2025-05-09')['results'],[])
        with self.assertRaises(Conflict):
            self.rag.record_image_metadata(job['id'],job['lease_epoch'],None,None,None,None)
        self.repo.delete_asset(self.asset['id'])
        self.assertIsNone(self.rag.image_metadata(self.asset['id'], self.asset['version_id']))

    def test_missing_capture_date_counts_only_indexed_images_in_requested_scope(self):
        data = b'example image bytes'
        path = self.repo.media_root / 'undated.jpg'
        path.write_bytes(data)
        self.asset = self.repo.save_file('미상', 'undated.jpg', 'image/jpeg', path, len(data), hashlib.sha256(data).hexdigest())
        self.space = self.rag.register_space('image-test', '1', 3, 'cosine', 'unit', 'p1', ['image'],
                                             document_encoder='doc', query_encoder='query')
        job = self.job()
        result = self.rag.create_result(job['id'], job['lease_epoch'])
        segments = [{'external_id':'full', 'kind':'image', 'locator':{'bbox':[0,0,1,1]},
                     'vectors':[{'ordinal':0,'values':[1,0,0]}]}]
        self.rag.submit_batch(result['id'],'image',job['lease_epoch'],self.asset['version_id'],self.space['id'],segments)
        self.rag.complete(result['id'],job['lease_epoch'],{'batches':['image'],'segment_count':1,'vector_count':1})
        found = self.rag.search_vector(self.space['id'],[1,0,0],kinds=['image'],captured_from='2025-01-01')
        self.assertEqual(found['results'], [])
        self.assertEqual(found['missing_capture_date_count'], 1)
        other = self.repo.create_collection('다른 컬렉션')
        scoped = self.rag.search_vector(self.space['id'],[1,0,0],kinds=['image'],collection_id=other['id'],captured_from='2025-01-01')
        self.assertNotIn('missing_capture_date_count', scoped)
        nonimage = self.rag.search_vector(self.space['id'],[1,0,0],kinds=['text'],captured_from='2025-01-01')
        self.assertNotIn('missing_capture_date_count', nonimage)
        self.repo.add_to_collection(other['id'],self.asset['id'])
        scoped = self.rag.search_vector(self.space['id'],[1,0,0],kinds=['image'],collection_id=other['id'],captured_to='2025-12-31')
        self.assertEqual(scoped['missing_capture_date_count'], 1)
        self.rag.set_collection_search_policy(self.space['id'], other['id'], 0.7)
        unrelated = self.rag.search_vector(self.space['id'],[0,1,0],kinds=['image'],collection_id=other['id'],captured_to='2025-12-31')
        self.assertNotIn('missing_capture_date_count', unrelated)
        relevant = self.rag.search_vector(self.space['id'],[1,0,0],kinds=['image'],collection_id=other['id'],captured_to='2025-12-31')
        self.assertEqual(relevant['missing_capture_date_count'], 1)

    def test_collection_search_policy_is_scoped_by_collection_and_model_space(self):
        job = self.job()
        result = self.rag.create_result(job['id'], job['lease_epoch'])
        self.batch(result, job)
        self.rag.complete(result['id'],job['lease_epoch'],{'batches':['batch-1'],'segment_count':1,'vector_count':1})
        first = self.repo.create_collection('사진')
        second = self.repo.create_collection('다른 사진')
        self.repo.add_to_collection(first['id'],self.asset['id'])
        self.repo.add_to_collection(second['id'],self.asset['id'])
        self.rag.set_collection_search_policy(self.space['id'], first['id'], 0.7)
        query = [0.6,0.8,0]
        self.assertEqual(self.rag.search_vector(self.space['id'],query,collection_id=first['id'])['results'], [])
        self.assertEqual(len(self.rag.search_vector(self.space['id'],query,collection_id=first['id'],
                                                    use_collection_policy=False)['results']), 1)
        self.assertEqual(len(self.rag.search_vector(self.space['id'],query,collection_id=second['id'])['results']), 1)
        self.assertEqual(len(self.rag.search_vector(self.space['id'],query)['results']), 1)
        self.assertEqual(self.rag.search_vector(self.space['id'],[1,0,0],collection_id=first['id'])['min_similarity'],0.7)
        self.rag.set_collection_search_policy(self.space['id'], first['id'], 0.5)
        self.assertEqual(len(self.rag.search_vector(self.space['id'],query,collection_id=first['id'])['results']), 1)
        other_space = self.rag.register_space('other','1',3,'cosine','unit','p1',['text'],document_encoder='doc',query_encoder='query')
        self.space = other_space
        job2 = self.job()
        result2 = self.rag.create_result(job2['id'],job2['lease_epoch'])
        self.batch(result2,job2)
        self.rag.complete(result2['id'],job2['lease_epoch'],{'batches':['batch-1'],'segment_count':1,'vector_count':1})
        self.assertEqual(len(self.rag.search_vector(other_space['id'],query,collection_id=first['id'])['results']), 1)
        for bad in (True, float('nan'), float('inf'), -1.1, 1.1):
            with self.assertRaises(ValueError):
                self.rag.set_collection_search_policy(other_space['id'],first['id'],bad)
        l2 = self.rag.register_space('l2','1',3,'l2','unit','p1',['text'],document_encoder='doc',query_encoder='query')
        with self.assertRaises(ValueError):
            self.rag.set_collection_search_policy(l2['id'],first['id'],0.7)

    def test_auto_policy_requires_one_collection_covering_all_active_images(self):
        self.space = self.rag.register_space('images','1',3,'cosine','unit','p1',['image'],document_encoder='doc',query_encoder='query')
        def add_image(name, dated=False):
            data = name.encode()
            path = self.repo.media_root / (name + '.jpg'); path.write_bytes(data)
            self.asset = self.repo.save_file(name, name+'.jpg','image/jpeg',path,len(data),hashlib.sha256(data).hexdigest())
            job = self.job()
            if dated:
                self.rag.record_image_metadata(job['id'],job['lease_epoch'],'2025-03-01T10:00:00',None,None,None)
            result = self.rag.create_result(job['id'],job['lease_epoch'])
            segments = [{'external_id':'i','kind':'image','locator':{'bbox':[0,0,1,1]},'vectors':[{'ordinal':0,'values':[1,0,0]}]}]
            self.rag.submit_batch(result['id'],'b',job['lease_epoch'],self.asset['version_id'],self.space['id'],segments)
            self.rag.complete(result['id'],job['lease_epoch'],{'batches':['b'],'segment_count':1,'vector_count':1})
            return self.asset['id']
        first = self.repo.create_collection('one')
        second = self.repo.create_collection('two')
        photo1 = add_image('first', dated=True)
        self.repo.add_to_collection(first['id'],photo1)
        self.rag.set_collection_search_policy(self.space['id'],first['id'],0.7)
        query = [0.6,0.8,0]
        result = self.rag.search_vector(self.space['id'],query,kinds=['image'],auto_collection_policy=True)
        self.assertEqual(result['results'], [])
        self.assertTrue(result['policy_auto_selected'])
        browse = self.rag.browse_collection_images(self.space['id'],first['id'],require_unique_policy=True)
        self.assertEqual([row['asset_id'] for row in browse['results']], [photo1])
        photo2 = add_image('second')
        result = self.rag.search_vector(self.space['id'],query,kinds=['image'],auto_collection_policy=True)
        self.assertEqual(len(result['results']), 2)
        self.assertNotIn('min_similarity',result)
        self.assertTrue(result['relevance_unverified'])
        self.assertIsNone(self.rag.browse_collection_images(self.space['id'],first['id'],require_unique_policy=True))
        self.assertEqual(len(self.rag.browse_collection_images(self.space['id'],first['id'])['results']),1)
        self.repo.add_to_collection(first['id'],photo2)
        result = self.rag.search_vector(self.space['id'],[1,0,0],kinds=['image'],captured_from='2025-01-01',auto_collection_policy=True)
        self.assertEqual(len(result['results']),1)
        self.assertEqual(result['missing_capture_date_count'],1)
        self.repo.add_to_collection(second['id'],photo1)
        self.repo.add_to_collection(second['id'],photo2)
        self.rag.set_collection_search_policy(self.space['id'],second['id'],0.7)
        result = self.rag.search_vector(self.space['id'],query,kinds=['image'],auto_collection_policy=True)
        self.assertEqual(len(result['results']),2)
        self.assertNotIn('min_similarity',result)
        self.assertTrue(result['relevance_unverified'])
        self.assertIsNone(self.rag.browse_collection_images(self.space['id'],first['id'],require_unique_policy=True))

    def test_artifact_integrity_and_expired_lease(self):
        job = self.job()
        result = self.rag.create_result(job['id'],job['lease_epoch'])
        data = b'transcript'
        sha = hashlib.sha256(data).hexdigest()
        artifact = self.rag.begin_artifact(result['id'],job['lease_epoch'],'transcript','a.txt',
                                           'text/plain',len(data),sha,'extractor-1')
        self.rag.put_artifact_part(artifact['id'],job['lease_epoch'],0,data,sha)
        stage = self.repo.media_root / 'staging' / 'rag' / artifact['id'] / '0'
        stage.write_bytes(b'corrupted')
        with self.assertRaises(ValueError):
            self.rag.complete_artifact(artifact['id'],job['lease_epoch'])
        stage.write_bytes(data)
        with self.repo.connect() as conn:
            conn.execute("UPDATE rag_jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (job['id'],))
        with self.assertRaises(Conflict):
            self.rag.complete_artifact(artifact['id'],job['lease_epoch'])

    def test_manifest_rejects_missing_artifact(self):
        job = self.job()
        result = self.rag.create_result(job['id'],job['lease_epoch'])
        self.batch(result,job)
        with self.assertRaises(Conflict):
            self.rag.complete(result['id'],job['lease_epoch'],{'batches':['batch-1'],'segment_count':1,
                'vector_count':1,'artifact_ids':[str(uuid.uuid4())],'artifact_count':1})

    def test_progress_and_explicit_retry(self):
        job = self.job()
        self.assertEqual(self.rag.heartbeat(job['id'],job['lease_epoch'],45)['progress'],45)
        with self.assertRaises(ValueError):
            self.rag.heartbeat(job['id'],job['lease_epoch'],101)
        self.assertEqual(self.rag.fail(job['id'],job['lease_epoch'],'model_error',False)['state'],'failed')
        self.assertEqual(self.rag.retry(job['id'])['state'],'queued')
        second = self.rag.claim(['v1'],[self.space['id']])
        self.assertGreater(second['lease_epoch'],job['lease_epoch'])
        with self.assertRaises(Conflict):
            self.rag.heartbeat(job['id'],job['lease_epoch'])

    def test_worker_http_contract_and_authentication(self):
        self.rag.enqueue(self.asset['id'],self.space['id'],request_key=str(uuid.uuid4()))
        token = 'worker-test-token-1234567890abcdef'
        app = Starlette(routes=worker_routes(self.rag,token,'https://example.com'))
        async def run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url='https://example.com') as client:
                prefix = '/api/v1/worker'
                self.assertEqual((await client.post(prefix+'/jobs/claim',json={})).status_code,401)
                client.headers['Authorization'] = 'Bearer '+token
                response = await client.post(prefix+'/jobs/claim',json={'pipeline_revisions':['v1'],'space_ids':[self.space['id']]})
                self.assertEqual(response.status_code,200,response.text)
                job = response.json()
                joburl = prefix+'/jobs/'+job['id']
                source = await client.post(joburl+'/source-grant',json={'lease_epoch':job['lease_epoch']})
                self.assertEqual(source.json()['text_body'],'해변에서 산책')
                result = (await client.post(joburl+'/result-sets',json={'lease_epoch':job['lease_epoch']})).json()
                resulturl = prefix+'/result-sets/'+result['id']
                artifact_data = b'frame data for RAG'
                artifact_sha = hashlib.sha256(artifact_data).hexdigest()
                artifact = await client.post(resulturl+'/artifacts',json={'lease_epoch':job['lease_epoch'],
                    'kind':'frame','filename':'frame.jpg','mime_type':'image/jpeg','size_bytes':len(artifact_data),
                    'sha256':artifact_sha,'extractor_revision':'test-1'})
                self.assertEqual(artifact.status_code,200,artifact.text)
                artifact_id = artifact.json()['id']
                artifacturl = prefix+'/artifacts/'+artifact_id
                partheaders = {'X-Lease-Epoch':str(job['lease_epoch']),'X-Part-SHA256':artifact_sha}
                response = await client.put(artifacturl+'/parts/0',content=artifact_data,headers=partheaders)
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual((await client.put(artifacturl+'/parts/0',content=artifact_data,headers=partheaders)).status_code,200)
                response = await client.post(artifacturl+'/complete',json={'lease_epoch':job['lease_epoch']})
                self.assertEqual(response.status_code,200,response.text)
                data = {'lease_epoch':job['lease_epoch'],'source_version_id':self.asset['version_id'],
                        'space_id':self.space['id'],'segments':[{'external_id':'one','kind':'text','text':'해변',
                        'locator':{'char_start':0,'char_end':2},'vectors':[{'ordinal':0,'values':[1,0,0]}]}]}
                response = await client.put(resulturl+'/batches/one',json=data)
                self.assertEqual(response.status_code,200,response.text)
                response = await client.post(resulturl+'/complete',json={'lease_epoch':job['lease_epoch'],
                    'manifest':{'batches':['one'],'segment_count':1,'vector_count':1,
                                'artifact_ids':[artifact_id],'artifact_count':1}})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(self.rag.list_artifacts(self.asset['id'])[0]['id'],artifact_id)
                saved = self.rag.get_artifact(artifact_id)
                self.assertEqual((self.repo.media_root / saved['object_key']).read_bytes(),artifact_data)
                self.assertEqual((await client.post(joburl+'/heartbeat',json={'lease_epoch':job['lease_epoch']})).status_code,409)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
