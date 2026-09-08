import unittest
from win_collab.lead import validate_response


class LeadContractTests(unittest.TestCase):
    def test_strict_decision_binding(self):
        p={'kind':'permission','request_id':'p','context_hash':'h'}
        good={'request_id':'p','context_hash':'h','decision':'once','reason':'Reviewed','answers':[]}
        self.assertEqual(validate_response(p,good),good)
        for update in ({'decision':'always'},{'context_hash':'other'},{'reason':''},{'extra':True}):
            with self.subTest(update=update),self.assertRaises(ValueError):validate_response(p,{**good,**update})

    def test_prose_is_not_a_decision(self):
        with self.assertRaises(ValueError):
            validate_response({'kind':'review','request_id':'p','context_hash':'h'},'do not pass until tested')
