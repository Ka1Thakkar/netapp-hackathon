package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"time"

	_ "modernc.org/sqlite"
)

// Store wraps a SQLite-backed metadata store.
// It is safe for concurrent use by multiple goroutines.
type Store struct {
	db *sql.DB
}

// DefaultSQLitePath returns a default file path suitable for Dockerized runtime.
func DefaultSQLitePath() string {
	if p := os.Getenv("SQLITE_PATH"); p != "" {
		return p
	}
	// Fallback path: matches container default in Dockerfile.python
	return "/data/metadata.db"
}

// DefaultSQLiteDSN returns a DSN tuned for server usage with WAL and busy timeout.
// It enables foreign keys and sets a reasonable busy timeout.
func DefaultSQLiteDSN(path string) string {
	// See modernc.org/sqlite DSN docs for pragmas:
	// - _pragma=busy_timeout(5000) -> wait up to 5s for locks
	// - _pragma=journal_mode(WAL)  -> better concurrency for writers/readers
	// - _pragma=foreign_keys(ON)   -> enforce FK constraints
	// Use file: prefix to avoid issues with absolute paths.
	return fmt.Sprintf("file:%s?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(ON)", path)
}

// NewStore opens the SQLite database and ensures the schema exists.
func NewStore(ctx context.Context, dsn string) (*Store, error) {
	if dsn == "" {
		dsn = DefaultSQLiteDSN(DefaultSQLitePath())
	}
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}

	// Reasonable limits for a small service
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(8)
	db.SetConnMaxLifetime(30 * time.Minute)

	// Ensure we can reach the database
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}

	s := &Store{db: db}
	if err := s.ensureSchema(ctx); err != nil {
		_ = s.Close()
		return nil, err
	}
	return s, nil
}

// Close closes the underlying database.
func (s *Store) Close() error {
	if s.db != nil {
		return s.db.Close()
	}
	return nil
}

// ensureSchema creates tables and indexes if they do not exist.
// The schema is aligned with the README's minimal viable schema.
func (s *Store) ensureSchema(ctx context.Context) error {
	stmts := []string{
		// datasets
		`CREATE TABLE IF NOT EXISTS datasets (
			id               INTEGER PRIMARY KEY,
			name             TEXT NOT NULL,
			path_uri         TEXT NOT NULL,
			current_tier     TEXT NOT NULL DEFAULT 'hot',
			latency_slo_ms   INTEGER,
			size_bytes       INTEGER,
			owner            TEXT,
			created_at_ms    INTEGER NOT NULL,
			updated_at_ms    INTEGER NOT NULL
		);`,
		`CREATE INDEX IF NOT EXISTS idx_datasets_name ON datasets(name);`,
		`CREATE INDEX IF NOT EXISTS idx_datasets_current_tier ON datasets(current_tier);`,

		// replicas
		`CREATE TABLE IF NOT EXISTS replicas (
			id               INTEGER PRIMARY KEY,
			dataset_id       INTEGER NOT NULL,
			location         TEXT NOT NULL,
			storage_class    TEXT,
			checksum         TEXT,
			is_primary       INTEGER NOT NULL DEFAULT 0, -- 0=false, 1=true
			created_at_ms    INTEGER NOT NULL,
			updated_at_ms    INTEGER NOT NULL,
			FOREIGN KEY (dataset_id) REFERENCES datasets (id) ON DELETE CASCADE
		);`,
		`CREATE INDEX IF NOT EXISTS idx_replicas_dataset ON replicas(dataset_id);`,
		`CREATE INDEX IF NOT EXISTS idx_replicas_primary ON replicas(dataset_id, is_primary);`,

		// policies
		`CREATE TABLE IF NOT EXISTS policies (
			id                    INTEGER PRIMARY KEY,
			dataset_id            INTEGER,              -- NULL means global policy
			max_cost_per_gb_month REAL,
			latency_slo_ms        INTEGER,
			min_redundancy        INTEGER,
			encryption_required   INTEGER NOT NULL DEFAULT 0,
			region_preferences    TEXT,                 -- JSON
			created_at_ms         INTEGER NOT NULL,
			updated_at_ms         INTEGER NOT NULL,
			FOREIGN KEY (dataset_id) REFERENCES datasets (id) ON DELETE CASCADE
		);`,
		`CREATE INDEX IF NOT EXISTS idx_policies_dataset ON policies(dataset_id);`,

		// access_aggregates
		`CREATE TABLE IF NOT EXISTS access_aggregates (
			id               INTEGER PRIMARY KEY,
			dataset_id       INTEGER NOT NULL,
			window_start_ms  INTEGER NOT NULL,
			window_end_ms    INTEGER NOT NULL,
			read_count       INTEGER NOT NULL DEFAULT 0,
			write_count      INTEGER NOT NULL DEFAULT 0,
			bytes_read       INTEGER NOT NULL DEFAULT 0,
			bytes_written    INTEGER NOT NULL DEFAULT 0,
			last_access_ts   INTEGER,
			FOREIGN KEY (dataset_id) REFERENCES datasets (id) ON DELETE CASCADE
		);`,
		`CREATE INDEX IF NOT EXISTS idx_access_agg_dataset_window ON access_aggregates(dataset_id, window_start_ms, window_end_ms);`,

		// recommendations
		`CREATE TABLE IF NOT EXISTS recommendations (
			id                   INTEGER PRIMARY KEY,
			dataset_id           INTEGER NOT NULL,
			recommended_tier     TEXT NOT NULL,
			recommended_location TEXT,
			reason               TEXT, -- JSON/text
			confidence           REAL NOT NULL DEFAULT 0.0,
			created_at_ms        INTEGER NOT NULL,
			FOREIGN KEY (dataset_id) REFERENCES datasets (id) ON DELETE CASCADE
		);`,
		`CREATE INDEX IF NOT EXISTS idx_reco_dataset ON recommendations(dataset_id);`,
		`CREATE INDEX IF NOT EXISTS idx_reco_created ON recommendations(created_at_ms);`,

		// migration_jobs
		`CREATE TABLE IF NOT EXISTS migration_jobs (
			id                 TEXT PRIMARY KEY,        -- job_id (UUID)
			job_key            TEXT UNIQUE,             -- idempotency key
			dataset_id         INTEGER NOT NULL,
			source_uri         TEXT NOT NULL,
			dest_uri           TEXT,
			dest_storage_class TEXT,
			status             TEXT NOT NULL,           -- queued|copying|verifying|switching|completed|failed|enqueue_failed
			error              TEXT,
			submitted_at_ms    INTEGER NOT NULL,
			updated_at_ms      INTEGER NOT NULL,
			FOREIGN KEY (dataset_id) REFERENCES datasets (id) ON DELETE CASCADE
		);`,
		`CREATE INDEX IF NOT EXISTS idx_jobs_status ON migration_jobs(status);`,
		`CREATE INDEX IF NOT EXISTS idx_jobs_dataset ON migration_jobs(dataset_id);`,

		// models (ML registry)
		`CREATE TABLE IF NOT EXISTS models (
			id            INTEGER PRIMARY KEY,
			name          TEXT NOT NULL,
			version       TEXT NOT NULL,
			algo          TEXT,
			params        TEXT,        -- JSON
			metrics       TEXT,        -- JSON
			created_at_ms INTEGER NOT NULL
		);`,
		`CREATE INDEX IF NOT EXISTS idx_models_name_version ON models(name, version);`,
	}

	for _, stmt := range stmts {
		if _, err := s.db.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("ensure schema failed: %w (stmt: %s)", err, stmt)
		}
	}
	return nil
}

//
// Datasets
//

// Dataset represents the datasets table row.
type Dataset struct {
	ID            int64
	Name          string
	PathURI       string
	CurrentTier   string
	LatencySloMs  sql.NullInt64
	SizeBytes     sql.NullInt64
	Owner         sql.NullString
	CreatedAtMs   int64
	UpdatedAtMs   int64
}

// UpsertDataset inserts or replaces a dataset.
// If d.ID == 0, it will insert a new row and set the ID.
func (s *Store) UpsertDataset(ctx context.Context, d *Dataset) error {
	now := nowMs()
	if d.CreatedAtMs == 0 {
		d.CreatedAtMs = now
	}
	d.UpdatedAtMs = now

	if d.ID == 0 {
		res, err := s.db.ExecContext(ctx, `
			INSERT INTO datasets (name, path_uri, current_tier, latency_slo_ms, size_bytes, owner, created_at_ms, updated_at_ms)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
			d.Name, d.PathURI, d.CurrentTier, nullInt64Ptr(d.LatencySloMs), nullInt64Ptr(d.SizeBytes), nullStringPtr(d.Owner), d.CreatedAtMs, d.UpdatedAtMs)
		if err != nil {
			return fmt.Errorf("insert dataset: %w", err)
		}
		id, _ := res.LastInsertId()
		d.ID = id
		return nil
	}

	_, err := s.db.ExecContext(ctx, `
		UPDATE datasets
		   SET name=?, path_uri=?, current_tier=?, latency_slo_ms=?, size_bytes=?, owner=?, updated_at_ms=?
		 WHERE id=?`,
		d.Name, d.PathURI, d.CurrentTier, nullInt64Ptr(d.LatencySloMs), nullInt64Ptr(d.SizeBytes), nullStringPtr(d.Owner), d.UpdatedAtMs, d.ID)
	if err != nil {
		return fmt.Errorf("update dataset: %w", err)
	}
	return nil
}

// GetDataset fetches a dataset by ID.
func (s *Store) GetDataset(ctx context.Context, id int64) (*Dataset, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT id, name, path_uri, current_tier, latency_slo_ms, size_bytes, owner, created_at_ms, updated_at_ms
		  FROM datasets WHERE id=?`, id)
	var d Dataset
	if err := row.Scan(&d.ID, &d.Name, &d.PathURI, &d.CurrentTier, &d.LatencySloMs, &d.SizeBytes, &d.Owner, &d.CreatedAtMs, &d.UpdatedAtMs); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, nil
		}
		return nil, fmt.Errorf("get dataset: %w", err)
	}
	return &d, nil
}

// ListDatasets returns up to limit datasets with offset.
func (s *Store) ListDatasets(ctx context.Context, limit, offset int) ([]*Dataset, error) {
	if limit <= 0 {
		limit = 100
	}
	if offset < 0 {
		offset = 0
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, name, path_uri, current_tier, latency_slo_ms, size_bytes, owner, created_at_ms, updated_at_ms
		  FROM datasets
		 ORDER BY id ASC
		 LIMIT ? OFFSET ?`, limit, offset)
	if err != nil {
		return nil, fmt.Errorf("list datasets: %w", err)
	}
	defer rows.Close()

	var out []*Dataset
	for rows.Next() {
		var d Dataset
		if err := rows.Scan(&d.ID, &d.Name, &d.PathURI, &d.CurrentTier, &d.LatencySloMs, &d.SizeBytes, &d.Owner, &d.CreatedAtMs, &d.UpdatedAtMs); err != nil {
			return nil, fmt.Errorf("scan dataset: %w", err)
		}
		out = append(out, &d)
	}
	return out, rows.Err()
}

//
// Migration Jobs
//

// UpsertMigrationJob inserts or replaces a migration job row.
func (s *Store) UpsertMigrationJob(ctx context.Context, j *MigrationJob) error {
	if j.JobID == "" {
		return errors.New("job_id required")
	}
	if j.SubmittedAt == 0 {
		j.SubmittedAt = nowMs()
	}
	if j.UpdatedAt == 0 {
		j.UpdatedAt = j.SubmittedAt
	}

	_, err := s.db.ExecContext(ctx, `
		INSERT INTO migration_jobs (id, job_key, dataset_id, source_uri, dest_uri, dest_storage_class, status, error, submitted_at_ms, updated_at_ms)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(id) DO UPDATE SET
			job_key=excluded.job_key,
			dataset_id=excluded.dataset_id,
			source_uri=excluded.source_uri,
			dest_uri=excluded.dest_uri,
			dest_storage_class=excluded.dest_storage_class,
			status=excluded.status,
			error=excluded.error,
			submitted_at_ms=excluded.submitted_at_ms,
			updated_at_ms=excluded.updated_at_ms
	`, j.JobID, j.JobKey, j.DatasetID, j.SourceURI, j.DestURI, j.DestStorageClass, j.Status, j.Error, j.SubmittedAt, j.UpdatedAt)
	if err != nil {
		return fmt.Errorf("upsert migration_job: %w", err)
	}
	return nil
}

// GetMigrationJob returns a job by ID, or nil if not found.
func (s *Store) GetMigrationJob(ctx context.Context, id string) (*MigrationJob, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT id, job_key, dataset_id, source_uri, dest_uri, dest_storage_class, status, error, submitted_at_ms, updated_at_ms
		  FROM migration_jobs
		 WHERE id=?`, id)

	var j MigrationJob
	if err := row.Scan(&j.JobID, &j.JobKey, &j.DatasetID, &j.SourceURI, &j.DestURI, &j.DestStorageClass, &j.Status, &j.Error, &j.SubmittedAt, &j.UpdatedAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, nil
		}
		return nil, fmt.Errorf("get migration_job: %w", err)
	}
	return &j, nil
}

// ListMigrationJobs returns a paginated list of jobs ordered by submitted time descending.
func (s *Store) ListMigrationJobs(ctx context.Context, limit, offset int) ([]*MigrationJob, error) {
	if limit <= 0 {
		limit = 100
	}
	if offset < 0 {
		offset = 0
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, job_key, dataset_id, source_uri, dest_uri, dest_storage_class, status, error, submitted_at_ms, updated_at_ms
		  FROM migration_jobs
		 ORDER BY submitted_at_ms DESC
		 LIMIT ? OFFSET ?`, limit, offset)
	if err != nil {
		return nil, fmt.Errorf("list migration_jobs: %w", err)
	}
	defer rows.Close()

	var out []*MigrationJob
	for rows.Next() {
		var j MigrationJob
		if err := rows.Scan(&j.JobID, &j.JobKey, &j.DatasetID, &j.SourceURI, &j.DestURI, &j.DestStorageClass, &j.Status, &j.Error, &j.SubmittedAt, &j.UpdatedAt); err != nil {
			return nil, fmt.Errorf("scan migration_job: %w", err)
		}
		out = append(out, &j)
	}
	return out, rows.Err()
}

// UpdateMigrationJobStatus sets status and optionally error message for a job.
// It also updates updated_at_ms. Returns sql.ErrNoRows-like error if the job does not exist.
func (s *Store) UpdateMigrationJobStatus(ctx context.Context, id string, status JobStatus, errMsg string) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE migration_jobs
		   SET status=?, error=?, updated_at_ms=?
		 WHERE id=?`, status, nullableErr(errMsg), nowMs(), id)
	if err != nil {
		return fmt.Errorf("update migration_job status: %w", err)
	}
	aff, _ := res.RowsAffected()
	if aff == 0 {
		return sql.ErrNoRows
	}
	return nil
}

// RecordMigrationStatus is a helper that maps a MigrationJob into the DB for each transition.
// Use this in tandem with produce-to-Kafka or as a fallback persistence audit.
func (s *Store) RecordMigrationStatus(ctx context.Context, j *MigrationJob) error {
	j.UpdatedAt = nowMs()
	return s.UpsertMigrationJob(ctx, j)
}

//
// Helpers
//

func nullStringPtr(ns sql.NullString) any {
	if ns.Valid {
		return ns.String
	}
	return nil
}

func nullInt64Ptr(ni sql.NullInt64) any {
	if ni.Valid {
		return ni.Int64
	}
	return nil
}

func nullableErr(s string) any {
	if s == "" {
		return nil
	}
	return s
}
