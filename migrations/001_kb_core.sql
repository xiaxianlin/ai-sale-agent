-- 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
-- 001_kb_core.sql：Phase 0 核心三表（依据 docs/02-设计文档/03-数据库设计.md §3.1）
-- 幂等执行：IF NOT EXISTS。P2 起业务表带 account_id（NFR-SCA-01 由 archguard 守护）。

-- ── 知识条目（六大知识库共用，kb_type 区分）──────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_item (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  uid             UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  tenant_id       BIGINT NOT NULL DEFAULT 1,
  account_id      BIGINT NOT NULL DEFAULT 0,
  kb_type         TEXT NOT NULL CHECK (kb_type IN
                    ('product','customer','skill','process','negative_case','training')),
  title           TEXT NOT NULL,
  content         TEXT NOT NULL,
  metadata        JSONB NOT NULL DEFAULT '{}',
  status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','published','archived')),
  current_version INT NOT NULL DEFAULT 1,
  sync_status     TEXT NOT NULL DEFAULT 'pending',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by      TEXT NOT NULL DEFAULT '',
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by      TEXT NOT NULL DEFAULT '',
  deleted_at      TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS idx_ki_type_status ON knowledge_item(kb_type, status)
  WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ki_metadata ON knowledge_item USING GIN (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_ki_updated ON knowledge_item(updated_at DESC, uid)
  WHERE deleted_at IS NULL;

-- ── 知识版本（快照链：UNIQUE(item,version) 幂等；rollback_from 标记回滚来源）──
CREATE TABLE IF NOT EXISTS knowledge_version (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  item_id       BIGINT NOT NULL REFERENCES knowledge_item(id),
  version       INT NOT NULL,
  snapshot      JSONB NOT NULL,
  change_note   TEXT NOT NULL DEFAULT '',
  rollback_from INT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by    TEXT NOT NULL DEFAULT '',
  UNIQUE (item_id, version)
);
CREATE INDEX IF NOT EXISTS idx_kv_item ON knowledge_version(item_id, version DESC);

-- ── 清洗流水留痕（七段：parse/dedup/correct/mask/chunk/embed/index）────────
CREATE TABLE IF NOT EXISTS ingest_pipeline_log (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_id        TEXT NOT NULL DEFAULT '',
  source_file     TEXT NOT NULL,
  source_format   TEXT NOT NULL CHECK (source_format IN
                    ('pdf','word','excel','image_ocr','audio_asr')),
  kb_type         TEXT NOT NULL DEFAULT '',
  stage           TEXT NOT NULL CHECK (stage IN
                    ('parse','dedup','correct','mask','chunk','embed','index')),
  status          TEXT NOT NULL DEFAULT 'success',
  action_detail   JSONB NOT NULL DEFAULT '{}',
  output_item_ids BIGINT[] NOT NULL DEFAULT '{}',
  error           TEXT NOT NULL DEFAULT '',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ipl_batch ON ingest_pipeline_log(batch_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ipl_source ON ingest_pipeline_log(source_file, stage);
