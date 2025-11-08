package main

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"

	_ "modernc.org/sqlite"
	"github.com/segmentio/kafka-go"
)

// -----------------------------------------------------------------------------
// Environment / Configuration
// -----------------------------------------------------------------------------

var (
	AppName         = env("APP_NAME", "data-in-motion-go-mover")
	AppVersion      = env("APP_VERSION", "0.1.0")
	Host            = env("HOST", "0.0.0.0")
	Port            = env("PORT", "8090")
	LogLevel        = env("LOG_LEVEL", "INFO")
	KafkaBrokers    = env("KAFKA_BROKERS", "localhost:9092")
	JobsTopic       = env("KAFKA_MIGRATION_JOBS_TOPIC", "migration_jobs")
	StatusTopic     = env("KAFKA_MIGRATION_STATUS_TOPIC", "migration_status")
	KafkaGroup      = env("KAFKA_GROUP", "go_mover_group")
	DisableKafka    = strings.ToLower(env("DISABLE_KAFKA", "false")) == "true"
	ShutdownGrace   = parseDuration(env("SHUTDOWN_GRACE_SECONDS", "10"))
	PollInterval    = parseDuration(env("JOB_CONSUMER_POLL_INTERVAL_MS", "500"))
	SimulateCycles  = parseInt(env("JOB_SIMULATION_CYCLES", "4"))
	JobsSQLitePath  = env("JOBS_SQLITE_PATH", "/shared_storage/jobs.db")
	JobsPragmaWAL   = strings.ToLower(env("JOBS_SQLITE_WAL", "true")) == "true"
	JobsPragmaSFK   = strings.ToLower(env("JOBS_SQLITE_FOREIGN_KEYS", "true")) == "true"
	JobsPragmaSync  = env("JOBS_SQLITE_SYNCHRONOUS", "NORMAL") // OFF|NORMAL|FULL|EXTRA
	JobsPragmaCache = env("JOBS_SQLITE_CACHE_SIZE", "-2000")    // negative => KiB
)

// -----------------------------------------------------------------------------
// Utilities
// -----------------------------------------------------------------------------

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func parseDuration(s string) time.Duration {
	d, err := time.ParseDuration(s + "ms")
	if err == nil {
		return d
	}
	// try as seconds
	if sec, err2 := time.ParseDuration(s + "s"); err2 == nil {
		return sec
	}
	// fallback
	if v, err3 := time.ParseDuration(s); err3 == nil {
		return v
	}
	return 5 * time.Second
}

func parseInt(s string) int {
	var out int
	_, err := fmt.Sscanf(s, "%d", &out)
	if err != nil {
		return 0
	}
	return out
}

func nowUnix() int64 { return time.Now().Unix() }
func nowMs() int64   { return time.Now().UnixNano() / int64(time.Millisecond) }

// -----------------------------------------------------------------------------
// Data Structures
// -----------------------------------------------------------------------------

type JobStatus string

const (
	JobQueued     JobStatus = "queued"
	JobCopying    JobStatus = "copying"
	JobVerifying  JobStatus = "verifying"
	JobSwitching  JobStatus = "switching"
	JobCompleted  JobStatus = "completed"
	JobFailed     JobStatus = "failed"
	JobEnqueueErr JobStatus = "enqueue_failed"
)

type MigrationJob struct {
	JobID            string    `json:"job_id"`
	JobKey           string    `json:"job_key,omitempty"`
	DatasetID        int       `json:"dataset_id"`
	SourceURI        string    `json:"source_uri"`
	DestURI          string    `json:"dest_uri,omitempty"`
	DestStorageClass string    `json:"dest_storage_class,omitempty"`
	Status           JobStatus `json:"status"`
	Error            string    `json:"error,omitempty"`
	SubmittedAt      int64     `json:"submitted_at"`
	UpdatedAt        int64     `json:"updated_at"`
	CyclesCompleted  int       `json:"cycles_completed"`
}

// SafeJobsMap wraps a jobs map with RWMutex
type SafeJobsMap struct {
	mu   sync.RWMutex
	data map[string]*MigrationJob
}

func NewSafeJobsMap() *SafeJobsMap {
	return &SafeJobsMap{data: make(map[string]*MigrationJob)}
}

func (m *SafeJobsMap) Get(id string) (*MigrationJob, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	j, ok := m.data[id]
	return j, ok
}

func (m *SafeJobsMap) List() []*MigrationJob {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := make([]*MigrationJob, 0, len(m.data))
	for _, j := range m.data {
		out = append(out, j)
	}
	return out
}

func (m *SafeJobsMap) Upsert(j *MigrationJob) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.data[j.JobID] = j
	// Persist if store initialized
	if jobStore != nil {
		if err := jobStore.Save(j); err != nil {
			logErr("job store save failed for %s: %v", j.JobID, err)
		}
	}
}

func (m *SafeJobsMap) UpdateStatus(id string, status JobStatus, errMsg string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	j, ok := m.data[id]
	if !ok {
		return errors.New("job not found")
	}
	j.Status = status
	j.Error = errMsg
	j.UpdatedAt = nowMs()

	// Persist status if store initialized
	if jobStore != nil {
		if err := jobStore.UpdateStatus(id, status, errMsg, j.UpdatedAt, j.CyclesCompleted); err != nil {
			logErr("job store update failed for %s: %v", id, err)
		}
	}
	return nil
}

// Global in-memory store
var Jobs = NewSafeJobsMap()

// -----------------------------------------------------------------------------
// Persistent Store (SQLite)
// -----------------------------------------------------------------------------

type JobStore struct {
	db *sql.DB
}

var jobStore *JobStore

func NewJobStore(path string) (*JobStore, error) {
	// modernc sqlite DSN is simply the file path
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)

	if err := initPragmas(db); err != nil {
		return nil, err
	}
	if err := initSchema(db); err != nil {
		return nil, err
	}
	return &JobStore{db: db}, nil
}

func initPragmas(db *sql.DB) error {
	if JobsPragmaWAL {
		if _, err := db.Exec(`PRAGMA journal_mode=WAL`); err != nil {
			return err
		}
	}
	if JobsPragmaSFK {
		if _, err := db.Exec(`PRAGMA foreign_keys=ON`); err != nil {
			return err
		}
	}
	if _, err := db.Exec(fmt.Sprintf(`PRAGMA synchronous=%s`, JobsPragmaSync)); err != nil {
		return err
	}
	if _, err := db.Exec(fmt.Sprintf(`PRAGMA cache_size=%s`, JobsPragmaCache)); err != nil {
		return err
	}
	return nil
}

func initSchema(db *sql.DB) error {
	ddl := `
CREATE TABLE IF NOT EXISTS migration_jobs (
  job_id TEXT PRIMARY KEY,
  job_key TEXT,
  dataset_id INTEGER,
  source_uri TEXT,
  dest_uri TEXT,
  dest_storage_class TEXT,
  status TEXT,
  error TEXT,
  submitted_at INTEGER,
  updated_at INTEGER,
  cycles_completed INTEGER
);
`
	_, err := db.Exec(ddl)
	return err
}

func (s *JobStore) Save(j *MigrationJob) error {
	_, err := s.db.Exec(
		`INSERT INTO migration_jobs
		 (job_id, job_key, dataset_id, source_uri, dest_uri, dest_storage_class, status, error, submitted_at, updated_at, cycles_completed)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		 ON CONFLICT(job_id) DO UPDATE SET
		   job_key=excluded.job_key,
		   dataset_id=excluded.dataset_id,
		   source_uri=excluded.source_uri,
		   dest_uri=excluded.dest_uri,
		   dest_storage_class=excluded.dest_storage_class,
		   status=excluded.status,
		   error=excluded.error,
		   submitted_at=excluded.submitted_at,
		   updated_at=excluded.updated_at,
		   cycles_completed=excluded.cycles_completed`,
		j.JobID, j.JobKey, j.DatasetID, j.SourceURI, j.DestURI, j.DestStorageClass, string(j.Status), j.Error, j.SubmittedAt, j.UpdatedAt, j.CyclesCompleted,
	)
	return err
}

func (s *JobStore) UpdateStatus(id string, status JobStatus, errMsg string, updatedAt int64, cycles int) error {
	_, err := s.db.Exec(
		`UPDATE migration_jobs SET status=?, error=?, updated_at=?, cycles_completed=? WHERE job_id=?`,
		string(status), errMsg, updatedAt, cycles, id,
	)
	return err
}

func (s *JobStore) Get(id string) (*MigrationJob, error) {
	row := s.db.QueryRow(
		`SELECT job_id, job_key, dataset_id, source_uri, dest_uri, dest_storage_class, status, error, submitted_at, updated_at, cycles_completed
		   FROM migration_jobs WHERE job_id=?`,
		id,
	)
	var j MigrationJob
	var status string
	if err := row.Scan(&j.JobID, &j.JobKey, &j.DatasetID, &j.SourceURI, &j.DestURI, &j.DestStorageClass, &status, &j.Error, &j.SubmittedAt, &j.UpdatedAt, &j.CyclesCompleted); err != nil {
		return nil, err
	}
	j.Status = JobStatus(status)
	return &j, nil
}

func (s *JobStore) List() ([]*MigrationJob, error) {
	rows, err := s.db.Query(
		`SELECT job_id, job_key, dataset_id, source_uri, dest_uri, dest_storage_class, status, error, submitted_at, updated_at, cycles_completed
		   FROM migration_jobs ORDER BY submitted_at DESC`,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []*MigrationJob
	for rows.Next() {
		var j MigrationJob
		var status string
		if err := rows.Scan(&j.JobID, &j.JobKey, &j.DatasetID, &j.SourceURI, &j.DestURI, &j.DestStorageClass, &status, &j.Error, &j.SubmittedAt, &j.UpdatedAt, &j.CyclesCompleted); err != nil {
			return nil, err
		}
		j.Status = JobStatus(status)
		out = append(out, &j)
	}
	return out, rows.Err()
}

func (s *JobStore) Close() error {
	return s.db.Close()
}

// -----------------------------------------------------------------------------
// Logging helpers
// -----------------------------------------------------------------------------

func logInfo(format string, args ...interface{}) {
	log.Printf("[INFO] "+format, args...)
}

func logWarn(format string, args ...interface{}) {
	log.Printf("[WARN] "+format, args...)
}

func logErr(format string, args ...interface{}) {
	log.Printf("[ERROR] "+format, args...)
}

// -----------------------------------------------------------------------------
// Kafka (Stub / Placeholder)
// -----------------------------------------------------------------------------

// KafkaAdapter interface allows swapping implementations (real vs stub).
type KafkaAdapter interface {
	Start(ctx context.Context) error
	Close(ctx context.Context) error
	ProduceStatus(ctx context.Context, job *MigrationJob) error
	RunJobsConsumer(ctx context.Context) error
}

// StubKafka implements no-op Kafka for local dev without broker.
type StubKafka struct{}

func (s *StubKafka) Start(ctx context.Context) error {
	logWarn("Kafka disabled; using stub adapter.")
	return nil
}

func (s *StubKafka) Close(ctx context.Context) error {
	logInfo("StubKafka closed.")
	return nil
}

func (s *StubKafka) ProduceStatus(ctx context.Context, job *MigrationJob) error {
	// For stub, just log
	b, _ := json.Marshal(job)
	logInfo("StubKafka ProduceStatus: %s", string(b))
	return nil
}

func (s *StubKafka) RunJobsConsumer(ctx context.Context) error {
	// In real implementation, consume from migration_jobs topic.
	// Here we just log and exit.
	logInfo("StubKafka RunJobsConsumer loop started.")
	<-ctx.Done()
	logInfo("StubKafka RunJobsConsumer loop stopped.")
	return nil
}

// RealKafka implements a KafkaAdapter using segmentio/kafka-go.
// It consumes migration_jobs messages and produces migration_status updates.
type RealKafka struct {
	brokers     []string
	jobsTopic   string
	statusTopic string

	writer *kafka.Writer
	reader *kafka.Reader
}

func NewRealKafka(brokersCSV, jobsTopic, statusTopic string) *RealKafka {
	brokers := strings.Split(brokersCSV, ",")
	return &RealKafka{
		brokers:     brokers,
		jobsTopic:   jobsTopic,
		statusTopic: statusTopic,
	}
}

func (rk *RealKafka) Start(ctx context.Context) error {
	dialer := &kafka.Dialer{
		Timeout:   10 * time.Second,
		DualStack: true,
	}

	// Proactively create topics if they do not exist (idempotent).
	// Some single-node dev brokers may not auto-create on first read.
	brokerAddr := rk.brokers[0]
	for _, t := range []string{rk.jobsTopic, rk.statusTopic} {
		conn, err := dialer.DialContext(ctx, "tcp", brokerAddr)
		if err != nil {
			logWarn("Topic creation dial failed for %s: %v", t, err)
			continue
		}
		// Attempt to fetch partitions; if error assume topic missing and create.
		_, partsErr := conn.ReadPartitions(t)
		if partsErr != nil {
			logInfo("Creating topic %s (missing): %v", t, partsErr)
			tc := kafka.TopicConfig{
				Topic:             t,
				NumPartitions:     1,
				ReplicationFactor: 1,
			}
			if createErr := conn.CreateTopics(tc); createErr != nil {
				logWarn("CreateTopics failed for %s: %v", t, createErr)
			}
		}
		_ = conn.Close()
	}

	rk.writer = &kafka.Writer{
		Addr:                   kafka.TCP(rk.brokers...),
		Topic:                  rk.statusTopic,
		Balancer:               &kafka.LeastBytes{},
		RequiredAcks:           kafka.RequireAll,
		AllowAutoTopicCreation: true,
		Async:                  false,
	}

	rk.reader = kafka.NewReader(kafka.ReaderConfig{
		Brokers:        rk.brokers,
		Topic:          rk.jobsTopic,
		GroupID:        KafkaGroup,
		Dialer:         dialer,
		GroupBalancers: []kafka.GroupBalancer{kafka.RangeGroupBalancer{}, kafka.RoundRobinGroupBalancer{}},
		StartOffset:    kafka.FirstOffset,
		MaxWait:        500 * time.Millisecond,
		MinBytes:       1,
		MaxBytes:       10e6,
		CommitInterval: time.Second,
	})

	logInfo("RealKafka started (jobs=%s status=%s brokers=%v)", rk.jobsTopic, rk.statusTopic, rk.brokers)
	return nil
}

func (rk *RealKafka) Close(ctx context.Context) error {
	if rk.reader != nil {
		if err := rk.reader.Close(); err != nil {
			logErr("Kafka reader close error: %v", err)
		}
	}
	if rk.writer != nil {
		if err := rk.writer.Close(); err != nil {
			logErr("Kafka writer close error: %v", err)
		}
	}
	logInfo("RealKafka closed.")
	return nil
}

func (rk *RealKafka) ProduceStatus(ctx context.Context, job *MigrationJob) error {
	payload := map[string]interface{}{
		"job_id":     job.JobID,
		"job_key":    job.JobKey,
		"dataset_id": job.DatasetID,
		"status":     job.Status,
		"error":      job.Error,
		"ts":         float64(job.UpdatedAt) / 1000.0,
	}
	b, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal status: %w", err)
	}
	msg := kafka.Message{
		Key:   []byte(job.JobID),
		Value: b,
		Time:  time.Now(),
	}
	if err := rk.writer.WriteMessages(ctx, msg); err != nil {
		return fmt.Errorf("write status: %w", err)
	}
	return nil
}

func (rk *RealKafka) RunJobsConsumer(ctx context.Context) error {
	logInfo("RealKafka consumer loop starting (topic=%s)", rk.jobsTopic)
	for {
		m, err := rk.reader.ReadMessage(ctx)
		if err != nil {
			if errors.Is(err, context.Canceled) {
				break
			}
			logErr("Kafka read error: %v", err)
			// Small backoff
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Second):
				continue
			}
		}
		var payload map[string]interface{}
		if err := json.Unmarshal(m.Value, &payload); err != nil {
			logWarn("Invalid job message: %v value=%s", err, string(m.Value))
			continue
		}
		jobID, _ := payload["job_id"].(string)
		datasetIDFloat, _ := payload["dataset_id"].(float64)
		sourceURI, _ := payload["source_uri"].(string)
		destURI, _ := payload["dest_uri"].(string)
		destClass, _ := payload["dest_storage_class"].(string)
		jobKey, _ := payload["job_key"].(string)

		if jobID == "" {
			logWarn("Job message missing job_id: %v", payload)
			continue
		}

		// Build job struct; initial status queued
		j := &MigrationJob{
			JobID:            jobID,
			JobKey:           jobKey,
			DatasetID:        int(datasetIDFloat),
			SourceURI:        sourceURI,
			DestURI:          destURI,
			DestStorageClass: destClass,
			Status:           JobQueued,
			SubmittedAt:      nowMs(),
			UpdatedAt:        nowMs(),
			CyclesCompleted:  0,
		}
		// Persist to memory
		Jobs.Upsert(j)
		// Persist to DB if available
		if jobStore != nil {
			if err := jobStore.Save(j); err != nil {
				logErr("Persist job %s error: %v", j.JobID, err)
			}
		}
		// Kick off progress simulation
		go simulateJobProgress(ctx, rk, j)
	}
	logInfo("RealKafka consumer loop stopped.")
	return nil
}

// -----------------------------------------------------------------------------
// Simulation / Processing
// -----------------------------------------------------------------------------

// simulateJobProgress emulates state transitions for a migration job.
func simulateJobProgress(ctx context.Context, kafka KafkaAdapter, job *MigrationJob) {
	// Local helper closures (no new imports required)
	isFileURI := func(s string) bool { return strings.HasPrefix(s, "file://") }
	pathFromURI := func(s string) string { return strings.TrimPrefix(s, "file://") }

	// SHA-256 streaming checksum of a file
		fileChecksumSHA256 := func(path string) (string, error) {
			f, err := os.Open(path)
			if err != nil {
				return "", fmt.Errorf("open for checksum: %w", err)
			}
			defer f.Close()
			h := sha256.New()
			buf := make([]byte, 32*1024)
			for {
				n, er := f.Read(buf)
				if n > 0 {
					_, _ = h.Write(buf[:n])
				}
				if er != nil {
					if er == io.EOF {
						break
					}
					return "", fmt.Errorf("read for checksum: %w", er)
				}
			}
			return fmt.Sprintf("%x", h.Sum(nil)), nil
		}

		// Copy a single local file using streaming read -> temp write -> atomic rename while computing SHA-256
		copyLocalFile := func(srcURI, dstURI string) (string, error) {
			src := pathFromURI(srcURI)
			dst := pathFromURI(dstURI)

			in, err := os.Open(src)
			if err != nil {
				return "", fmt.Errorf("open source: %w", err)
			}
			defer in.Close()

			if err := os.MkdirAll(path.Dir(dst), 0o755); err != nil {
				return "", fmt.Errorf("mkdir: %w", err)
			}

			tmp := dst + ".tmp"
			out, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
			if err != nil {
				return "", fmt.Errorf("open tmp: %w", err)
			}

			h := sha256.New()
			buf := make([]byte, 64*1024)
			for {
				n, er := in.Read(buf)
				if n > 0 {
					chunk := buf[:n]
					_, _ = h.Write(chunk)
					if _, we := out.Write(chunk); we != nil {
						out.Close()
						_ = os.Remove(tmp)
						return "", fmt.Errorf("write tmp: %w", we)
					}
				}
				if er != nil {
					if er == io.EOF {
						break
					}
					out.Close()
					_ = os.Remove(tmp)
					return "", fmt.Errorf("read source: %w", er)
				}
			}
			if err := out.Sync(); err != nil {
				out.Close()
				_ = os.Remove(tmp)
				return "", fmt.Errorf("sync tmp: %w", err)
			}
			if err := out.Close(); err != nil {
				_ = os.Remove(tmp)
				return "", fmt.Errorf("close tmp: %w", err)
			}
			if err := os.Rename(tmp, dst); err != nil {
				_ = os.Remove(tmp)
				return "", fmt.Errorf("rename: %w", err)
			}

			checksum := fmt.Sprintf("%x", h.Sum(nil))
			return checksum, nil
		}

		// Read a file back and compute SHA-256 checksum
		readChecksum := func(uri string) (string, error) {
			p := pathFromURI(uri)
			return fileChecksumSHA256(p)
		}

	// If this looks like a local file copy, attempt a real copy + verify
	if isFileURI(job.SourceURI) && isFileURI(job.DestURI) {
		// 1) copying
		job.CyclesCompleted++
		job.Status = JobCopying
		job.UpdatedAt = nowMs()
		if err := kafka.ProduceStatus(ctx, job); err != nil {
			logErr("Failed to produce status for job=%s: %v", job.JobID, err)
		} else {
			logInfo("Job %s transitioned to %s", job.JobID, JobCopying)
		}
		Jobs.Upsert(job)

		expectedSum, err := copyLocalFile(job.SourceURI, job.DestURI)
		if err != nil {
			job.Status = JobFailed
			job.Error = fmt.Sprintf("copy error: %v", err)
			job.UpdatedAt = nowMs()
			_ = kafka.ProduceStatus(ctx, job)
			Jobs.Upsert(job)
			return
		}

		// 2) verifying
		job.CyclesCompleted++
		job.Status = JobVerifying
		job.UpdatedAt = nowMs()
		if err := kafka.ProduceStatus(ctx, job); err != nil {
			logErr("Failed to produce status for job=%s: %v", job.JobID, err)
		} else {
			logInfo("Job %s transitioned to %s", job.JobID, JobVerifying)
		}
		Jobs.Upsert(job)

		gotSum, err := readChecksum(job.DestURI)
		if err != nil || gotSum != expectedSum {
			job.Status = JobFailed
			if err != nil {
				job.Error = fmt.Sprintf("verify error: %v", err)
			} else {
				job.Error = fmt.Sprintf("checksum mismatch: expected=%s got=%s", expectedSum, gotSum)
			}
			job.UpdatedAt = nowMs()
			_ = kafka.ProduceStatus(ctx, job)
			Jobs.Upsert(job)
			return
		}

		// 3) switching (placeholder: would update primary replica/pointers in DB)
		job.CyclesCompleted++
		job.Status = JobSwitching
		job.UpdatedAt = nowMs()
		if err := kafka.ProduceStatus(ctx, job); err != nil {
			logErr("Failed to produce status for job=%s: %v", job.JobID, err)
		} else {
			logInfo("Job %s transitioned to %s", job.JobID, JobSwitching)
		}
		Jobs.Upsert(job)

		// 4) completed
		job.CyclesCompleted++
		job.Status = JobCompleted
		job.UpdatedAt = nowMs()
		if err := kafka.ProduceStatus(ctx, job); err != nil {
			logErr("Failed to produce status for job=%s: %v", job.JobID, err)
		} else {
			logInfo("Job %s transitioned to %s", job.JobID, JobCompleted)
		}
		Jobs.Upsert(job)
		return
	}

	// Fallback to time-based simulation if not a local file copy
	ticker := time.NewTicker(PollInterval)
	defer ticker.Stop()

	sequence := []JobStatus{JobCopying, JobVerifying, JobSwitching, JobCompleted}
	for _, st := range sequence {
		select {
		case <-ctx.Done():
			logWarn("Simulation aborted for job=%s", job.JobID)
			return
		case <-ticker.C:
			job.CyclesCompleted++
			job.Status = st
			job.UpdatedAt = nowMs()
			if err := kafka.ProduceStatus(ctx, job); err != nil {
				logErr("Failed to produce status for job=%s: %v", job.JobID, err)
			} else {
				logInfo("Job %s transitioned to %s", job.JobID, st)
			}
			Jobs.Upsert(job)
		}
	}
}

// retryJob resets failed job to queued and re-simulates.
func retryJob(ctx context.Context, kafka KafkaAdapter, jobID string) (*MigrationJob, error) {
	j, ok := Jobs.Get(jobID)
	if !ok {
		// try DB as source of truth
		if jobStore != nil {
			dbJob, err := jobStore.Get(jobID)
			if err == nil {
				Jobs.Upsert(dbJob)
				j = dbJob
				ok = true
			}
		}
	}
	if !ok {
		return nil, errors.New("job not found")
	}
	if j.Status != JobFailed {
		return nil, fmt.Errorf("job not in failed state, current=%s", j.Status)
	}
	j.Status = JobQueued
	j.Error = ""
	j.CyclesCompleted = 0
	j.UpdatedAt = nowMs()
	Jobs.Upsert(j)
	go simulateJobProgress(ctx, kafka, j)
	return j, nil
}

// -----------------------------------------------------------------------------
// HTTP Handlers
// -----------------------------------------------------------------------------

func healthHandler(w http.ResponseWriter, r *http.Request) {
	count := len(Jobs.List())
	// If DB present, prefer DB count for accuracy
	if jobStore != nil {
		if list, err := jobStore.List(); err == nil {
			count = len(list)
		}
	}
	resp := map[string]interface{}{
		"status":       "ok",
		"app":          AppName,
		"version":      AppVersion,
		"go_version":   runtime.Version(),
		"jobs_count":   count,
		"kafka_broker": KafkaBrokers,
		"time":         time.Now().UTC().Format(time.RFC3339Nano),
		"db_path":      JobsSQLitePath,
	}
	writeJSON(w, http.StatusOK, resp)
}

func listJobsHandler(w http.ResponseWriter, r *http.Request) {
	// Prefer DB listing if available
	if jobStore != nil {
		if items, err := jobStore.List(); err == nil {
			writeJSON(w, http.StatusOK, items)
			return
		}
	}
	writeJSON(w, http.StatusOK, Jobs.List())
}

func getJobHandler(w http.ResponseWriter, r *http.Request) {
	// Extract last segment as job id
	id := path.Base(r.URL.Path)
	if id == "jobs" || id == "" {
		http.Error(w, "missing job id", http.StatusBadRequest)
		return
	}
	// Try in-memory
	if j, ok := Jobs.Get(id); ok {
		writeJSON(w, http.StatusOK, j)
		return
	}
	// Try DB
	if jobStore != nil {
		if j, err := jobStore.Get(id); err == nil {
			// hydrate memory cache
			Jobs.Upsert(j)
			writeJSON(w, http.StatusOK, j)
			return
		}
	}
	http.Error(w, "job not found", http.StatusNotFound)
}

func retryJobHandler(ctx context.Context, kafka KafkaAdapter) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		id := path.Base(r.URL.Path)
		if id == "" {
			http.Error(w, "missing job id", http.StatusBadRequest)
			return
		}
		j, err := retryJob(ctx, kafka, id)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		writeJSON(w, http.StatusOK, j)
	}
}

// writeJSON helper
func writeJSON(w http.ResponseWriter, code int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		logErr("failed to encode json: %v", err)
	}
}

// -----------------------------------------------------------------------------
// Server / Routing
// -----------------------------------------------------------------------------

func newMux(ctx context.Context, kafka KafkaAdapter) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", healthHandler)
	mux.HandleFunc("/jobs", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/jobs" {
			if r.Method == http.MethodGet {
				listJobsHandler(w, r)
				return
			}
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		// /jobs/{id}
		if strings.HasPrefix(r.URL.Path, "/jobs/") && r.Method == http.MethodGet {
			getJobHandler(w, r)
			return
		}
		http.Error(w, "not found", http.StatusNotFound)
	})
	mux.Handle("/jobs/retry/", retryJobHandler(ctx, kafka))
	return mux
}

// -----------------------------------------------------------------------------
// Bootstrap / Main
// -----------------------------------------------------------------------------

func main() {
	logInfo("Starting %s version=%s", AppName, AppVersion)
	rootCtx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Setup signal handling
	sigCh := make(chan os.Signal, 2)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	// Initialize persistent store
	var err error
	jobStore, err = NewJobStore(JobsSQLitePath)
	if err != nil {
		logErr("Failed to open job store at %s: %v (continuing without persistence)", JobsSQLitePath, err)
	} else {
		// hydrate in-memory cache
		if items, lerr := jobStore.List(); lerr == nil {
			for _, j := range items {
				Jobs.Upsert(j)
			}
			logInfo("Loaded %d jobs from store", len(items))
		}
	}

	// Initialize Kafka adapter (stub for now)
	var kafka KafkaAdapter
	if DisableKafka {
		kafka = &StubKafka{}
	} else {
		// Prepare real Kafka adapter; defer actual start to unified start call below.
		kafka = NewRealKafka(KafkaBrokers, JobsTopic, StatusTopic)
	}

	// Start exactly once (stub or real). On failure, downgrade to stub.
	if err := kafka.Start(rootCtx); err != nil {
		logErr("Kafka start failed: %v (falling back to stub)", err)
		kafka = &StubKafka{}
		_ = kafka.Start(rootCtx)
	}

	// Jobs consumer (stubbed)
	go func() {
		if err := kafka.RunJobsConsumer(rootCtx); err != nil && !errors.Is(err, context.Canceled) {
			logErr("Jobs consumer error: %v", err)
		}
	}()

	// Example: Seed a job for demonstration if not exists
	const seedID = "demo-job-1"
	if _, ok := Jobs.Get(seedID); !ok {
		seedJob := &MigrationJob{
			JobID:       seedID,
			JobKey:      "dataset:1:ts:seed",
			DatasetID:   1,
			SourceURI:   "file:///data/source/dataset1",
			DestURI:     "file:///data/dest/dataset1",
			Status:      JobQueued,
			SubmittedAt: nowMs(),
			UpdatedAt:   nowMs(),
		}
		Jobs.Upsert(seedJob)
		go simulateJobProgress(rootCtx, kafka, seedJob)
	}

	// HTTP server
	mux := newMux(rootCtx, kafka)
	srv := &http.Server{
		Addr:         Host + ":" + Port,
		Handler:      logRequestMiddleware(mux),
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	go func() {
		logInfo("HTTP server listening on %s", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logErr("Server error: %v", err)
			cancel()
		}
	}()

	// Wait for signal
	sig := <-sigCh
	logWarn("Received signal %v, initiating shutdown...", sig)
	cancel()

	ctxShutdown, cancelShutdown := context.WithTimeout(context.Background(), ShutdownGrace)
	defer cancelShutdown()

	if err := srv.Shutdown(ctxShutdown); err != nil {
		logErr("HTTP server graceful shutdown failed: %v", err)
	} else {
		logInfo("HTTP server stopped gracefully.")
	}

	if err := kafka.Close(ctxShutdown); err != nil {
		logErr("Kafka close error: %v", err)
	}

	if jobStore != nil {
		_ = jobStore.Close()
	}

	logInfo("Shutdown complete.")
}

// -----------------------------------------------------------------------------
// Middleware
// -----------------------------------------------------------------------------

func logRequestMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		ww := &responseWriter{ResponseWriter: w, status: 200}
		next.ServeHTTP(ww, r)
		dur := time.Since(start)
		logInfo("%s %s status=%d duration_ms=%d", r.Method, r.URL.Path, ww.status, dur.Milliseconds())
	})
}

// responseWriter captures status codes.
type responseWriter struct {
	http.ResponseWriter
	status int
}

func (rw *responseWriter) WriteHeader(code int) {
	rw.status = code
	rw.ResponseWriter.WriteHeader(code)
}

// -----------------------------------------------------------------------------
// End
// -----------------------------------------------------------------------------
