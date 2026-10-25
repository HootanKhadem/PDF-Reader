from pathlib import Path
import re

import pymupdf
import pytest

from app import core


def test_four_columns_read_top_to_bottom_before_next_column():
    with pymupdf.open() as doc:
        page=doc.new_page(width=600,height=800)
        for x,prefix in zip((40,180,320,460),('ALPHA','BRAVO','CHARLIE','DELTA')):
            page.insert_text((x,100),'\n'.join(prefix+str(i) for i in range(10)),fontsize=10)
        text,_=core.extract_page(page)
        assert text.index('ALPHA9')<text.index('BRAVO0')
        assert text.index('BRAVO9')<text.index('CHARLIE0')
        assert text.index('CHARLIE9')<text.index('DELTA0')


def test_native_stat_labels_and_numeric_cells_are_preserved():
    # PDF extractors can emit each table cell as a separate text block.
    class FragmentedPage:
        rect=pymupdf.Rect(0,0,600,800)
        def get_text(self,*args):
            texts=['A shield protects a traveler from physical attacks and incoming blows.','HP','17','AC','12']
            return [(50,50+i*40,200,70+i*40,text,i,0) for i,text in enumerate(texts)]
        def get_texttrace(self):
            return [{'type':0}]
    text,used_ocr=core.extract_page(FragmentedPage())
    assert not used_ocr
    assert 'HP' in text and '17' in text and 'AC' in text and '12' in text


@pytest.mark.skipif(not Path('/books/Monster Manual.pdf').exists(),reason='User-supplied regression PDF absent')
def test_reported_artwork_noise_and_four_column_list():
    with pymupdf.open('/books/Monster Manual.pdf') as doc:
        text,used_ocr=core.extract_page(doc[19])
        assert used_ocr
        assert 'ANIMATED FLYING SWORD' in text.upper()
        assert 'ANIMATED RUG OF SMOTHERING' in text.upper()
        assert re.search(r'\bHP\s+14\b',text)
        assert re.search(r'\bHP\s+27\b',text)
        lines=[line for line in text.splitlines() if line.strip()]
        assert sum(len(line.strip())<=2 for line in lines)<5
        text,used_ocr=core.extract_page(doc[381])
        assert used_ocr
        assert 'ABERRATION' in text.upper() and 'CONSTRUCT' in text.upper()
        assert text.index('Aboleth')<text.index('Brown Bear')<text.index('Mastiff')<text.index('Animated Armor')
