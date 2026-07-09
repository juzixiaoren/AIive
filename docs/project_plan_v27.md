# AIive project_plan_v27.md：Temporal Memory Graph v2：Graphiti/Zep-style Adapter 与时间有效性

> 前置要求：V26 完成。  
> 本阶段目标：在 memory_records 真相源之上引入 Temporal KG。  
> 重点：KG 是派生关系层，不是 memory 生命周期真相源。

---

## 1. 技术选型

```text
第一步：PostgreSQL graph_entities / graph_relations 表。
Adapter 接口：TemporalGraphAdapter。
后续可接 Graphiti/Zep-style Temporal KG。
禁止直接把外部 KG 当 truth source。
```

---

## 2. 必须实现

```text
memory_records -> graph projector
graph_entities(entity_id, name, type, source_memory_ids, confidence)
graph_relations(relation_id, subject_id, predicate, object_id, valid_from, valid_to, source_memory_ids, status)
superseded memory -> expire relation
forgotten memory -> redact/remove relation
```

---

## 3. Chat 入口

```text
你为什么认为我叫 B？
我和这个项目之间有什么关系？
哪些信息已经过期了？
```

---

## 4. 验收

```text
A -> B 称呼更新后，KG 中 A 关系过期，B 关系有效。
Context Evidence Pack 可注入关系摘要。
KG 可由 memory_records 重建。
```
