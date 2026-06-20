const tslib = require('tslib');
exports.handler = async (event) => {
  return { statusCode: 200, body: tslib.__assign({}, { ok: true }) };
};
