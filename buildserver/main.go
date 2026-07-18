// build-server: a tiny, statically-linked command-execution HTTP server.
//
// It runs inside the run-agent pod as a *separate container* alongside the
// Python agent, on a stock Haskell toolchain image (e.g. docker.io/haskell:9.10).
// The agent reaches it over the pod's localhost network and POSTs cabal commands
// here, so the untrusted build (third-party library code: custom Setup.hs,
// Template Haskell, build hooks) runs in this container rather than the agent's.
//
// The binary is fully static (CGO_ENABLED=0, stdlib only), so it can be injected
// into any base image — an initContainer copies it onto a shared volume that the
// stock toolchain sidecar then executes. No per-toolchain image to maintain.
//
// Contract (matches refactor.py's builder_exec):
//
//	GET  /health  -> 200 {"ok": true}
//	POST /execute  {"command": [...], "cwd": "..."}
//	              -> 200 {"stdout": "...", "stderr": "...", "exitCode": N}
//
// This container holds no credentials; it shares only the workspace volume.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

type execRequest struct {
	Command []string `json:"command"`
	Cwd     string   `json:"cwd"`
}

type execResponse struct {
	Stdout   string `json:"stdout"`
	Stderr   string `json:"stderr"`
	ExitCode int    `json:"exitCode"`
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	port := env("PORT", "8080")
	defaultCwd := env("WORKDIR", "/workspace/repo")
	timeout := 3600
	if v, err := strconv.Atoi(os.Getenv("EXEC_TIMEOUT")); err == nil && v > 0 {
		timeout = v
	}

	mux := http.NewServeMux()

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
	})

	mux.HandleFunc("/execute", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST only"})
			return
		}
		var req execRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || len(req.Command) == 0 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "expected {command:[...], cwd}"})
			return
		}
		cwd := req.Cwd
		if cwd == "" {
			cwd = defaultCwd
		}
		log.Printf("exec %v (cwd=%s)", req.Command, cwd)

		ctx, cancel := context.WithTimeout(r.Context(), time.Duration(timeout)*time.Second)
		defer cancel()

		cmd := exec.CommandContext(ctx, req.Command[0], req.Command[1:]...)
		cmd.Dir = cwd
		var stdout, stderr bytes.Buffer
		cmd.Stdout = io.MultiWriter(os.Stdout, &stdout)
		cmd.Stderr = io.MultiWriter(os.Stderr, &stderr)
		err := cmd.Run()

		resp := execResponse{Stdout: stdout.String(), Stderr: stderr.String()}
		switch {
		case ctx.Err() == context.DeadlineExceeded:
			resp.Stderr += "\n[build-server] timed out after " + strconv.Itoa(timeout) + "s"
			resp.ExitCode = 124
		case err == nil:
			resp.ExitCode = 0
		default:
			if ee, ok := err.(*exec.ExitError); ok {
				resp.ExitCode = ee.ExitCode()
			} else {
				// Command could not start (e.g. binary not found).
				resp.Stderr += "\n[build-server] " + err.Error()
				resp.ExitCode = 127
			}
		}
		log.Printf("exit %d: %v", resp.ExitCode, req.Command)
		writeJSON(w, http.StatusOK, resp)
	})

	addr := ":" + port
	srv := &http.Server{Addr: addr, Handler: mux}

	// Graceful shutdown on SIGTERM/SIGINT: Argo/Kubernetes SIGTERMs the sidecar
	// once the main container finishes. Handle it explicitly so we exit 0 (as
	// PID 1 a Go process won't act on an un-handled SIGTERM), instead of hanging
	// until SIGKILL and being reported as an unclean exit.
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		sig := <-stop
		log.Printf("[build-server] received %s, shutting down", sig)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			log.Printf("[build-server] graceful shutdown failed: %v", err)
			_ = srv.Close()
		}
	}()

	log.Printf("[build-server] listening on %s (default cwd %s)", addr, defaultCwd)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("[build-server] server error: %v", err)
	}
	log.Printf("[build-server] stopped")
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
