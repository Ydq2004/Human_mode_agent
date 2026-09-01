import unittest

from memory.personality_traits import TRAIT_REGISTRY, get_trait_definition, list_all_traits


class PersonalityTraitsTests(unittest.TestCase):
    def test_registry_has_three_initial_traits(self):
        self.assertEqual(set(TRAIT_REGISTRY), {"openness", "conscientiousness", "extraversion"})
        self.assertEqual(len(list_all_traits()), 3)

    def test_each_trait_has_scale_and_descriptions(self):
        for trait in list_all_traits():
            self.assertGreater(trait.trait_delta_scale, 0)
            self.assertTrue(trait.low_description)
            self.assertTrue(trait.high_description)
            self.assertIs(get_trait_definition(trait.trait_id), trait)


if __name__ == "__main__":
    unittest.main()
