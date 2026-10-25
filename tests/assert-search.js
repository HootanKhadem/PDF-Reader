pm.test('Search succeeds', () => pm.response.to.have.status(200));
const data = pm.response.json();
pm.test('Qdrant vector backend', () => pm.expect(data.retrieval_backend).to.eql('qdrant'));
pm.test('Excerpt budget enforced', () => {
  pm.expect(data.text_tokens).to.be.above(0);
  pm.expect(data.text_tokens).to.be.at.most(800);
  pm.expect(data.results.reduce((sum, r) => sum + r.text_tokens, 0)).to.eql(data.text_tokens);
});
pm.test('Page citations and expansion IDs returned', () => {
  pm.expect(data.results.length).to.be.above(0);
  for (const result of data.results) {
    pm.expect(result.id).to.be.a('string');
    pm.expect(result.page).to.be.at.least(1);
    pm.expect(result.source_url).to.include('#page=' + result.page);
  }
});
