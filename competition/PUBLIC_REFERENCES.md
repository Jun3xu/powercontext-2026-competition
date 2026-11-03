# PDF 实现公开参考

以下为公开源码参考，链接固定到检查时的 commit；未核验这些仓库的正式比赛成绩。对应仓库均发布 Apache-2.0 许可证。

| 仓库与版本 | 可借鉴的方法 |
| --- | --- |
| [Point-yang/powercontext-competition](https://github.com/Point-yang/powercontext-competition/tree/eea10e3568db51de8d0640742cf32c5618526b32) | 完整捕获 PDF 页的独立 Memory 容量规则、权威来源投影核对、NFC 一致性、保守的布局提取回退 |
| [zzh20071105/PowerContext-competition](https://github.com/zzh20071105/PowerContext-competition/blob/a0ab39afd23dafd7cbbb43144fa24731a8f5db94/src/powercontext/sources/pdf_pages.py) | BM25、章节类型配额、伪相关反馈和排名融合 |
| [digod111/database-competition](https://github.com/digod111/database-competition/blob/7c93dbc4ce1f3bb2a5abd043d9a614db4e650c61/src/powercontext/sources/pdf_pages.py) | 对原问题及内容词分别进行真实 FTS 检索，再融合排名 |

本仓库的文本提取与长页持久化采用独立实现，并以构造 PDF 和真实 Runtime 回归验证相关行为。文本提取额外检查数字分组未被布局转换改变。检索使用 SQLite FTS5 Porter 统一词形，并采用有限章节覆盖和近重复降权；实现通过独立构造语料验证，不包含文档名或问题答案硬编码。

参考代码只提供方法与合同检查线索；本地测试不代表正式证据页召回门槛已经通过。未读取答案、证据标注、证据页清单或私有判题资产。

SQLite Porter 与 FTS5 词汇表接口参考：[官方 FTS5 文档](https://www.sqlite.org/fts5.html)。
