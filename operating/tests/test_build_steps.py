"""The build steps read every document safely."""

import unittest
class LoneSurrogateTests(unittest.TestCase):
    """One lone surrogate in one captured text failed a whole generation's mentions step
    (UnicodeEncodeError); the document steps read it as U+FFFD."""

    def test_a_lone_surrogate_reads_as_a_replacement_character(self):
        from ontology.mentions import Document, TextField
        from operating import build_steps
        doc = Document("graph-mail://mbx/MSG-1@CK-1", "email", (TextField("graph-mail://mbx/MSG-1@CK-1", "body", "ok \ud83d cut"),
                                     TextField("graph-mail://mbx/MSG-1@CK-1", "subject", "fine")),
                       copy_key=("mail", "id\udc80"))
        clean = build_steps.clean_document(doc)
        self.assertEqual(clean.fields[0].text, "ok � cut")
        self.assertIs(clean.fields[1], doc.fields[1])
        self.assertEqual(clean.copy_key, ("mail", "id�"))
        clean.fields[0].text.encode("utf-8")

    def test_a_clean_document_is_the_same_object(self):
        from ontology.mentions import Document, TextField
        from operating import build_steps
        doc = Document("graph-mail://mbx/MSG-1@CK-1", "email", (TextField("graph-mail://mbx/MSG-1@CK-1", "body", "emoji \U0001F600 ok"),),
                       copy_key=("mail", "x"))
        self.assertIs(build_steps.clean_document(doc), doc)

    def test_the_steps_see_cleaned_groups_and_the_rest_of_the_source(self):
        from ontology.mentions import Document, TextField, source_document_groups
        from operating import build_steps

        class Source:
            marker = 7
            def document_groups(self):
                yield "k", lambda: ([Document("graph-mail://mbx/MSG-1@CK-1", "email", (TextField("graph-mail://mbx/MSG-1@CK-1", "b", "a\ud800"),))], True)

        class Projector:
            sources = {"mail": Source()}
            build_context = "ctx"

        seen = {}
        def step(builder, projector):
            [(key, load)] = list(source_document_groups(projector))
            docs, remembered = load()
            seen.update(key=key, text=docs[0].fields[0].text, remembered=remembered,
                        ctx=projector.build_context, marker=projector.sources["mail"].marker)
            return {}
        build_steps._documents_cleaned(step)(None, Projector())
        self.assertEqual(seen, {"key": "k", "text": "a�", "remembered": True,
                                "ctx": "ctx", "marker": 7})


if __name__ == "__main__":
    unittest.main()
