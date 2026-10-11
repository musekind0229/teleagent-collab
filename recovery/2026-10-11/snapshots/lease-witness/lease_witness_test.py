import unittest
import lease_witness


class TestLeaseWitness(unittest.TestCase):
    def test_busy(self):
        self.assertEqual(lease_witness.classify_state("busy"), "hold")

    def test_available(self):
        self.assertEqual(lease_witness.classify_state("available"), "ready")

    def test_unknown(self):
        self.assertEqual(lease_witness.classify_state("other"), "unknown")


if __name__ == "__main__":
    unittest.main()
