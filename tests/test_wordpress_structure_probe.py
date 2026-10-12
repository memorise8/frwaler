import json
import unittest
from urllib.parse import urlencode

from delivery.scripts.probe_wordpress_structure import parse_metadata,verify,validate_report,FIELDS


class WordPressProbeTests(unittest.TestCase):
    def observations(self):
        item=lambda n:dict(id=str(n),date=f'2020-01-0{n}',source_datetime=f'2020-01-0{n}T10:00:00',url=f'https://source.example/publications/{n}')
        sample=lambda ids:dict(items=[item(i) for i in ids],total=5,pages=3)
        return [sample([5,4]),sample([1,2]),sample([1]),dict(code='rest_post_invalid_page_number',status=400),
                dict(items=[],total=0,pages=0),sample([1,2])]

    def test_two_orderings_end_error_filter_and_repeat_cross_confirm(self):
        result=verify(self.observations());self.assertEqual(result['oldest']['id'],'1');self.assertEqual(result['total'],5)

    def test_ignored_date_filter_does_not_pass(self):
        obs=self.observations();obs[4]=obs[0]
        with self.assertRaisesRegex(ValueError,'filter'):verify(obs)

    def test_arbitrary_HTTP400_is_not_source_exhaustion(self):
        obs=self.observations();obs[3]['code']='rest_invalid_param'
        with self.assertRaisesRegex(ValueError,'explicitly'):verify(obs)

    def test_different_orderings_or_changing_total_do_not_pass(self):
        obs=self.observations();obs[2]['items'][0]['source_datetime']='2021-01-01T10:00:00'
        with self.assertRaisesRegex(ValueError,'disagree'):verify(obs)
        obs=self.observations();obs[2]['total']=6
        with self.assertRaisesRegex(ValueError,'total'):verify(obs)

    def test_projection_and_header_contract_reject_document_body(self):
        response={'source_headers':{'X-WP-Total':'5','X-WP-TotalPages':'3'}}
        item={'id':1,'date':'2020-01-01T10:00:00','link':'https://source.example/publications/1'}
        summary,total,pages,items=parse_metadata(json.dumps([item]),response,'https://source.example/wp-json/wp/v2/publications')
        self.assertEqual((total,pages),(5,3));self.assertIn('not_research_publication_date',items[0]['date_basis'])
        item['content']={'rendered':'unexpected document body'}
        with self.assertRaisesRegex(ValueError,'projection'):parse_metadata(json.dumps([item]),response,'https://source.example/wp-json/wp/v2/publications')

    def test_evidence_query_cannot_silently_change_collection_scope(self):
        observations=self.observations();endpoint='https://source.example/wp-json/wp/v2/publications';responses=[]
        for index,item in enumerate(observations):
            query=dict(per_page=2,page=3 if index==2 else 4 if index==3 else 1,orderby='date',order='asc' if index in (1,5) else 'desc',_fields=FIELDS)
            if index==4:query['before']='2020-01-01T09:59:59'
            response=dict(url=endpoint+'?'+urlencode(query),status=400 if index==3 else 200,sha256='fixture')
            if index!=3:response['summary']=dict(items=item['items'],totals=[dict(value=item['total'])])
            responses.append(response)
        report=dict(status='passed',requests=responses,observations=observations,endpoint=endpoint)
        self.assertEqual(validate_report(report)['total'],5)
        report['requests'][2]['url']+='&categories=restricted'
        with self.assertRaisesRegex(ValueError,'scope'):validate_report(report)


if __name__=='__main__':unittest.main()
