
import { useEffect, useRef, useState } from "react";
import "./App.css";
// Render AI investigation reports as Markdown.
import ReactMarkdown from "react-markdown";

// Support GitHub-style Markdown, including tables and task lists.
import remarkGfm from "remark-gfm";

// By default this is same-origin and goes through Vite's loopback API proxy.
// An absolute URL is only for an intentionally configured deployment.
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

const TASK_LIFECYCLE_STAGES = [
  { key: "queued", label: "Queued" },
  { key: "policy_checked", label: "Policy Checked" },
  { key: "worker_provisioning", label: "Worker Ready" },
  { key: "checkout_ready", label: "Checkout Staged" },
  { key: "investigating", label: "Investigating" },
  { key: "patch_validating", label: "Validating Patch" },
  { key: "awaiting_approval", label: "Awaiting Approval" },
];

function App() {
  const [page, setPage] = useState("Overview");

  // Repository data is loaded from the real backend.
  const [repositories, setRepositories] = useState([]);
  const [repositoriesLoading, setRepositoriesLoading] = useState(true);
  const [repositoryError, setRepositoryError] = useState("");

  // Investigation history is loaded from the SQLite-backed API.
  const [recentInvestigations, setRecentInvestigations] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState("");

  const [repoId, setRepoId] = useState("");
  const [description, setDescription] = useState("");
  const [githubRepositoryId, setGithubRepositoryId] = useState("");
  const [githubRepositoryName, setGithubRepositoryName] = useState("");
  const [githubRepositoryUrl, setGithubRepositoryUrl] = useState("");
  const [githubImporting, setGithubImporting] = useState(false);
  const [githubImportError, setGithubImportError] = useState("");

  // Durable task inspection state
  const [taskIdInput, setTaskIdInput] = useState("");
  const [durableTask, setDurableTask] = useState(null);
  const [taskAuditEvents, setTaskAuditEvents] = useState([]);
  const [taskLoading, setTaskLoading] = useState(false);
  const [taskProcessing, setTaskProcessing] = useState(false);
  const [taskError, setTaskError] = useState("");
  const [taskSuccessMessage, setTaskSuccessMessage] = useState("");

  // Screenshot and error image state (multimodal AI analysis)
  const [image, setImage] = useState(null);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const fileInput = useRef(null);
  const cameraInput = useRef(null);

  // Track the actual investigation request and its response.
  const [investigating, setInvestigating] = useState(false);
  const [investigationResult, setInvestigationResult] = useState(null);
  const [investigationError, setInvestigationError] = useState("");

  // Patch approval and direct file application state
  const [applyingPatch, setApplyingPatch] = useState(false);
  const [patchApplyResult, setPatchApplyResult] = useState(null);
  const [patchApplyError, setPatchApplyError] = useState("");

  async function loadRepositories() {
    try {
      setRepositoriesLoading(true);
      setRepositoryError("");

      const response = await fetch(
        `${API_BASE_URL}/api/repositories`
      );

      if (!response.ok) {
        throw new Error(`Backend returned HTTP ${response.status}`);
      }

      const data = await response.json();

      if (!Array.isArray(data.repositories)) {
        throw new Error("Invalid repository list returned by the API.");
      }

      setRepositories(data.repositories);

      // Prefer the existing selection if it remains registered and connected.
      setRepoId((currentId) => {
        const currentRepo = data.repositories.find(
          (repo) =>
            repo.repository_id === currentId &&
            repo.status === "connected"
        );

        if (currentRepo) return currentId;

        const firstConnected = data.repositories.find(
          (repo) => repo.status === "connected"
        );

        return firstConnected?.repository_id ?? "";
      });
    } catch (error) {
      setRepositoryError(
        error instanceof Error
          ? error.message
          : "Could not load repositories."
      );
    } finally {
      setRepositoriesLoading(false);
    }
  }

  // Load the registered repositories when the app opens.
  useEffect(() => {
    loadRepositories();
  }, []);

  // Load real saved investigations from the backend.
  async function loadInvestigations() {
    try {
      setHistoryLoading(true);
      setHistoryError("");

      const response = await fetch(
        `${API_BASE_URL}/api/investigations?limit=50`
      );

      if (!response.ok) {
        throw new Error(`History API returned HTTP ${response.status}`);
      }

      const data = await response.json();

      if (!Array.isArray(data.investigations)) {
        throw new Error("Invalid investigation history returned by the API.");
      }

      setRecentInvestigations(data.investigations);
    } catch (error) {
      setHistoryError(
        error instanceof Error
          ? error.message
          : "Could not load investigation history."
      );
    } finally {
      setHistoryLoading(false);
    }
  }

  // Load history when the app opens.
  useEffect(() => {
    loadInvestigations();
  }, []);

  // Format saved timestamps for display in the history lists.
  function formatHistoryTime(timestamp) {
    if (!timestamp) return "Time unavailable";

    const date = new Date(timestamp);

    if (Number.isNaN(date.getTime())) {
      return "Time unavailable";
    }

    return date.toLocaleString();
  }

  // Use the bug description as the history title.
  function historyTitle(item) {
    const title = (item.description || "Untitled investigation").trim();
    return title.length > 90 ? `${title.slice(0, 87)}...` : title;
  }

  // Normalize backend statuses for the existing UI labels.
  function historyStatus(status) {
    if (status === "completed") return "Completed";
    if (status === "incomplete") return "Incomplete";
    if (status === "failed") return "Failed";
    return formatStatus(status);
  }

  async function loadDurableTask(idToLoad) {
    const id = (idToLoad || taskIdInput).trim();
    if (!id) {
      setTaskError("Please enter a valid task UUID.");
      return;
    }
    setTaskLoading(true);
    setTaskError("");
    setTaskSuccessMessage("");
    try {
      const response = await fetch(`${API_BASE_URL}/api/tasks/${encodeURIComponent(id)}`);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `Failed to fetch durable task (HTTP ${response.status})`);
      }
      setDurableTask(data.task);
      setTaskAuditEvents(data.audit_events || []);
    } catch (err) {
      setDurableTask(null);
      setTaskAuditEvents([]);
      setTaskError(err instanceof Error ? err.message : "Failed to load durable task.");
    } finally {
      setTaskLoading(false);
    }
  }

  async function handleProcessDurableTask(idToProcess) {
    const id = idToProcess || durableTask?.task_id;
    if (!id) return;
    setTaskProcessing(true);
    setTaskError("");
    setTaskSuccessMessage("");
    try {
      const response = await fetch(`${API_BASE_URL}/api/tasks/${encodeURIComponent(id)}/process`, {
        method: "POST",
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `Failed to process durable task (HTTP ${response.status})`);
      }
      setTaskSuccessMessage(`Task state advanced: now in '${data.task.state}'.`);
      await loadDurableTask(id);
    } catch (err) {
      setTaskError(err instanceof Error ? err.message : "Failed to advance durable task.");
    } finally {
      setTaskProcessing(false);
    }
  }

  function getStepStatus(stageKey, currentTaskState) {
    if (currentTaskState === "failed") return "failed";
    if (currentTaskState === "cancelled") return "cancelled";
    const stageOrder = [
      "queued",
      "policy_checked",
      "worker_provisioning",
      "checkout_ready",
      "investigating",
      "patch_validating",
      "awaiting_approval",
    ];
    const currentIndex = stageOrder.indexOf(currentTaskState);
    const stageIndex = stageOrder.indexOf(stageKey);
    if (stageIndex === -1 || currentIndex === -1) return "pending";
    if (stageIndex < currentIndex) return "completed";
    if (stageIndex === currentIndex) return "active";
    return "pending";
  }

  // Render unambiguous empirical verification status badges.
  function renderVerificationBadge(verification) {
    if (!verification) {
      return (
        <span className="history-status" title="Analysis only; unverified in sandbox">
          Analysis Only
        </span>
      );
    }

    const isVerified =
      verification.valid === true &&
      verification.reproduction_confirmed === true &&
      verification.fix_verified === true &&
      verification.regression_suite_passed === true;

    if (isVerified) {
      return (
        <span className="history-status verified-badge" title="Empirically verified in isolated sandbox">
          ✓ Verified with Test
        </span>
      );
    }

    if (verification.regression_suite_passed === false) {
      return (
        <span className="history-status failed" title="Patch broke existing regression tests">
          ⚠ Regression Failed
        </span>
      );
    }

    if (verification.regression_suite_passed == null && verification.reproduction_resolved === true) {
      return (
        <span className="history-status incomplete" title="The reproduction passed, but no complete regression suite was available">
          Regression Suite Missing
        </span>
      );
    }

    if (verification.fix_verified === false && verification.reproduction_confirmed === true) {
      return (
        <span className="history-status failed" title="Reproduction still failing after patch">
          Fix Unverified
        </span>
      );
    }

    if (verification.reproduction_confirmed === false) {
      return (
        <span className="history-status incomplete" title="Bug could not be reproduced in sandbox">
          Repro Unconfirmed
        </span>
      );
    }

    return (
      <span className="history-status failed" title="Sandbox verification failed">
        Verification Failed
      </span>
    );
  }

  // Release the preview URL when an image is replaced or the app unmounts.
  useEffect(() => {
    return () => {
      if (image?.previewUrl) {
        URL.revokeObjectURL(image.previewUrl);
      }
    };
  }, [image]);

  const selectedRepo = repositories.find(
    (repo) => repo.repository_id === repoId
  );

  // Clear stale results whenever the user changes investigation inputs.
  function clearResult() {
    setInvestigationResult(null);
    setInvestigationError("");
    setPatchApplyResult(null);
    setPatchApplyError("");
  }

  async function handleApplyPatch(targetRepoId, patchFile, patchedContent) {
    if (!targetRepoId || !patchFile || !patchedContent) return;
    setApplyingPatch(true);
    setPatchApplyError("");
    setPatchApplyResult(null);

    try {
      const response = await fetch(`${API_BASE_URL}/api/patches/apply`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          repository_id: targetRepoId,
          patch_file: patchFile,
          patched_content: patchedContent,
        }),
      });

      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `Failed to apply patch (HTTP ${response.status})`);
      }

      setPatchApplyResult({
        patchedFile: data.patched_file || patchFile,
        backupFile: data.backup_file || "",
        message: data.message || `Successfully applied fix to ${data.patched_file || patchFile}!`,
      });
    } catch (err) {
      setPatchApplyError(err instanceof Error ? err.message : "Failed to apply fix to repository.");
    } finally {
      setApplyingPatch(false);
    }
  }

  async function handlePublicGitHubImport(event) {
    event.preventDefault();
    setGithubImportError("");

    if (!githubRepositoryId.trim() || !githubRepositoryName.trim() || !githubRepositoryUrl.trim()) {
      setGithubImportError("Repository ID, display name, and public GitHub URL are required.");
      return;
    }

    setGithubImporting(true);
    try {
      const response = await fetch(`${API_BASE_URL}/api/repositories/github-public`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          repository_id: githubRepositoryId.trim(),
          name: githubRepositoryName.trim(),
          github_url: githubRepositoryUrl.trim(),
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.repository) {
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : `GitHub import failed with HTTP ${response.status}.`
        );
      }

      setRepositories((current) => [...current, data.repository]);
      setRepoId(data.repository.repository_id);
      setGithubRepositoryId("");
      setGithubRepositoryName("");
      setGithubRepositoryUrl("");
      clearResult();
    } catch (error) {
      setGithubImportError(
        error instanceof Error ? error.message : "Could not import the public GitHub repository."
      );
    } finally {
      setGithubImporting(false);
    }
  }

  function processImageFile(file) {
    if (!file) return;

    if (!file.type.startsWith("image/")) {
      alert("Please choose a valid image file (PNG, JPG, WebP, etc.).");
      return;
    }

    if (file.size > 12 * 1024 * 1024) {
      alert("Please choose an image smaller than 12 MB.");
      return;
    }

    const reader = new FileReader();
    reader.onload = (e) => {
      setImage({
        file,
        previewUrl: URL.createObjectURL(file),
        base64: e.target?.result,
      });
      clearResult();
    };
    reader.readAsDataURL(file);
  }

  function handleImageChange(event) {
    const file = event.target.files?.[0];
    if (file) {
      processImageFile(file);
    }
  }

  function handlePaste(event) {
    const items = event.clipboardData?.items;
    if (!items) return;
    for (let i = 0; i < items.length; i++) {
      if (items[i].type.startsWith("image/")) {
        const file = items[i].getAsFile();
        if (file) {
          processImageFile(file);
          break;
        }
      }
    }
  }

  function handleRemoveImage() {
    setImage(null);

    if (fileInput.current) {
      fileInput.current.value = "";
    }
    if (cameraInput.current) {
      cameraInput.current.value = "";
    }

    clearResult();
  }

  // Send the bug report and optional screenshot to FastAPI for multimodal agent analysis.
  async function handleSubmit(event) {
    event.preventDefault();
    clearResult();

    if (repositoriesLoading) {
      setInvestigationError("Wait for the repository list to load.");
      return;
    }

    if (repositoryError) {
      setInvestigationError(
        "The backend repository list is unavailable. Check FastAPI and retry."
      );
      return;
    }

    if (!selectedRepo || selectedRepo.status !== "connected") {
      setInvestigationError("Select a connected repository first.");
      return;
    }

    if (!description.trim() && !image) {
      setInvestigationError(
        "Describe the bug or attach an error screenshot before starting an investigation."
      );
      return;
    }

    setInvestigating(true);

    try {
      const response = await fetch(
        `${API_BASE_URL}/api/investigations`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            repository_id: repoId,
            description: description.trim(),
            image_data: image ? image.base64 : null,
          }),
        }
      );

      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        const message =
          typeof data.detail === "string"
            ? data.detail
            : `Investigation failed with HTTP ${response.status}.`;

        throw new Error(message);
      }

      setInvestigationResult(data);

      // The API saves the report before returning; refresh the history list.
      await loadInvestigations();
    } catch (error) {
      setInvestigationError(
        error instanceof Error
          ? error.message
          : "An unexpected investigation error occurred."
      );
    } finally {
      setInvestigating(false);
    }
  }

  function formatStatus(status) {
    if (!status) return "Unknown";
    return status.charAt(0).toUpperCase() + status.slice(1);
  }

  return (
    <div className="app-shell">
      {mobileNavOpen && (
        <div
          className="sidebar-backdrop"
          onClick={() => setMobileNavOpen(false)}
          aria-hidden="true"
        />
      )}

      <aside className={`sidebar ${mobileNavOpen ? "mobile-open" : ""}`}>
        <div className="sidebar-header-row">
          <a
            className="brand"
            href="#home"
            onClick={(event) => {
              event.preventDefault();
              setPage("Overview");
              setMobileNavOpen(false);
            }}
          >
            <span className="brand-icon">D</span>
            <span>
              <strong>DevPilot</strong>
              <small>AI debugging workspace</small>
            </span>
          </a>
          <button
            type="button"
            className="drawer-close-button"
            onClick={() => setMobileNavOpen(false)}
            aria-label="Close navigation menu"
          >
            ✕
          </button>
        </div>

        <p className="nav-label">WORKSPACE</p>

        {["Overview", "Investigations", "Repositories", "Durable Tasks"].map((item) => (
          <button
            key={item}
            className={`nav-item ${page === item ? "active" : ""}`}
            onClick={() => {
              setPage(item);
              setMobileNavOpen(false);
            }}
          >
            <span className="nav-dot" />
            {item}
          </button>
        ))}

        <div className="sidebar-bottom">
          <div className="connection-card">
            <span className="status-dot" />
            <div>
              <strong>Agent workspace</strong>
              <small>
                {repositoryError
                  ? "Backend connection issue"
                  : repositoriesLoading
                    ? "Connecting to backend..."
                    : "Repository API connected"}
              </small>
            </div>
          </div>
          <p className="muted tiny">DevPilot · Early access</p>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div className="topbar-left">
            <button
              type="button"
              className={`mobile-nav-toggle ${mobileNavOpen ? "open" : ""}`}
              aria-label="Toggle navigation menu"
              onClick={() => setMobileNavOpen((open) => !open)}
            >
              <span className="hamburger-line" />
              <span className="hamburger-line" />
              <span className="hamburger-line" />
            </button>
            <div className="breadcrumbs">
              Workspace <span>/</span> {page}
            </div>
          </div>
          <div className="user-avatar" title="Your workspace">A</div>
        </header>

        <div className="page-container">
          {page === "Overview" && (
            <>
              <section className="welcome-row">
                <div>
                  <p className="eyebrow">YOUR DEVELOPMENT WORKSPACE</p>
                  <h1>
                    Find the bug.
                    <br />
                    Understand the fix.
                  </h1>
                  <p className="subtitle">
                    Give DevPilot an error screenshot or description.
                    Start with evidence, finish with a verified solution.
                  </p>
                </div>

                <div className="hero-symbol" aria-hidden="true">
                  <span>⌘</span>
                  <i />
                </div>
              </section>

              <section className="panel investigation-panel">
                <div className="panel-heading">
                  <div>
                    <p className="eyebrow">NEW INVESTIGATION</p>
                    <h2>What are we debugging?</h2>
                  </div>
                  <span className="step-label">STEP 01</span>
                </div>

                <form onSubmit={handleSubmit}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <label className="field-label" htmlFor="repository" style={{ margin: "16px 0 8px" }}>
                      Target Repository
                    </label>
                    <button
                      type="button"
                      className="text-button"
                      style={{ fontSize: "11px", margin: "16px 0 8px" }}
                      onClick={() => loadRepositories()}
                      disabled={repositoriesLoading}
                    >
                      {repositoriesLoading ? "Refreshing..." : "↻ Refresh repos"}
                    </button>
                  </div>

                  <div className="select-container">
                    <select
                      id="repository"
                      className="repo-dropdown"
                      value={repoId}
                      onChange={(event) => {
                        setRepoId(event.target.value);
                        clearResult();
                      }}
                      disabled={repositoriesLoading || repositories.length === 0}
                    >
                      {repositoriesLoading && (
                        <option value="">Loading registered repositories...</option>
                      )}

                      {!repositoriesLoading && repositories.length === 0 && (
                        <option value="">No repositories registered yet</option>
                      )}

                      {!repositoriesLoading && repositories.length > 0 && !repoId && (
                        <option value="" disabled>Select a connected repository...</option>
                      )}

                      {repositories.map((repo) => (
                        <option
                          key={repo.repository_id}
                          value={repo.repository_id}
                          disabled={repo.status !== "connected"}
                        >
                          {repo.name} ({repo.source_type})
                          {repo.status !== "connected" ? " · Unavailable" : " · Connected"}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="repo-note">
                    <span className="status-dot" />
                    {repositoriesLoading
                      ? "Loading registered repositories..."
                      : repositoryError
                        ? "Could not retrieve repositories"
                        : selectedRepo
                          ? `${selectedRepo.name} · ${formatStatus(selectedRepo.status)}`
                          : "No connected repository found"}
                  </div>

                  {repositoryError && (
                    <p className="error-message" role="alert">
                      Could not load repositories: {repositoryError}
                    </p>
                  )}

                  <label className="field-label" htmlFor="bug-description">
                    Describe the problem or paste / attach error screenshot
                  </label>

                  <textarea
                    id="bug-description"
                    rows="4"
                    placeholder="Describe the bug, paste a screenshot (Ctrl+V), or snap a photo of the error..."
                    value={description}
                    onPaste={handlePaste}
                    onChange={(event) => {
                      setDescription(event.target.value);
                      clearResult();
                    }}
                  />

                  <div className="upload-area">
                    <input
                      ref={fileInput}
                      type="file"
                      accept="image/*"
                      hidden
                      onChange={handleImageChange}
                    />
                    <input
                      ref={cameraInput}
                      type="file"
                      accept="image/*"
                      capture="environment"
                      hidden
                      onChange={handleImageChange}
                    />

                    {image ? (
                      <div className="image-preview">
                        <img
                          src={image.previewUrl}
                          alt="Bug screenshot preview"
                        />

                        <div className="image-info">
                          <strong>{image.file.name || "Attached Screenshot"}</strong>
                          <small>
                            {(image.file.size / 1024).toFixed(0)} KB · Ready for Multimodal Analysis
                          </small>

                          <button
                            type="button"
                            className="text-button"
                            onClick={handleRemoveImage}
                          >
                            Remove image
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="upload-triggers-container">
                        <button
                          type="button"
                          className="upload-trigger"
                          onClick={() => fileInput.current?.click()}
                        >
                          <span className="upload-icon">📁</span>
                          <span>
                            <strong>Upload screenshot</strong>
                            <small>
                              Pick from files or gallery · or paste (Ctrl+V)
                            </small>
                          </span>
                          <span className="upload-plus">+</span>
                        </button>

                        <button
                          type="button"
                          className="upload-trigger camera-trigger"
                          onClick={() => cameraInput.current?.click()}
                        >
                          <span className="upload-icon">📷</span>
                          <span>
                            <strong>Snap Photo</strong>
                            <small>Use phone camera</small>
                          </span>
                          <span className="upload-plus">+</span>
                        </button>
                      </div>
                    )}
                  </div>

                  <div className="form-footer">
                    <span className="muted tiny">
                      ✨ Multimodal AI: Screenshots will be transcribed and analyzed alongside repository code.
                    </span>

                    <button
                      className="primary-button"
                      type="submit"
                      disabled={
                        investigating ||
                        repositoriesLoading ||
                        Boolean(repositoryError) ||
                        !selectedRepo ||
                        selectedRepo.status !== "connected"
                      }
                    >
                      {investigating ? "Investigating..." : "Investigate bug"}
                      <span>{investigating ? "…" : "↗"}</span>
                    </button>
                  </div>
                </form>

                {investigating && (
                  <div className="demo-notice" role="status" aria-live="polite">
                    <strong>Investigation in progress.</strong> DevPilot is
                    searching the selected repository and analyzing relevant
                    source files. This may take a little while.
                  </div>
                )}

                {investigationError && (
                  <div className="error-message" role="alert">
                    <strong>Investigation failed.</strong>{" "}
                    {investigationError}
                  </div>
                )}

                {investigationResult && (
                  <section className="section-block" aria-live="polite">
                    <div className="section-heading">
                      <div>
                        <p className="eyebrow">INVESTIGATION RESULTS</p>
                        <h2>
                          {investigationResult.status === "completed"
                            ? "Findings"
                            : investigationResult.status === "failed"
                              ? "Investigation failed"
                              : "Investigation incomplete"}
                        </h2>
                      </div>

                      <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                        {renderVerificationBadge(investigationResult.verification)}
                        <span
                          className={`history-status ${
                            investigationResult.status === "completed"
                              ? "resolved"
                              : investigationResult.status === "failed"
                                ? "failed"
                                : "incomplete"
                          }`}
                        >
                          {historyStatus(investigationResult.status)}
                        </span>
                      </div>
                    </div>

                    <p className="muted tiny">
                      Repository:{" "}
                      {investigationResult.repository_name || repoId}
                      {" · "}
                      Agent iterations: {investigationResult.iterations ?? "—"}
                    </p>

                    {investigationResult.verification && (
                      <section className="verification-card">
                        <div className="verification-header">
                          <div className="verification-title">
                            <span>🛡️</span>
                            <strong>Empirical Sandbox Verification</strong>
                          </div>
                          {renderVerificationBadge(investigationResult.verification)}
                        </div>

                        <div className="verification-meta">
                          <div className="verification-stat">
                            <span>Analysis Status</span>
                            <strong>
                              {investigationResult.verification.analysis_passed ? "Analysis Passed" : "Incomplete"}
                            </strong>
                          </div>
                          <div className="verification-stat">
                            <span>Baseline Reproduction</span>
                            <strong>
                              {investigationResult.verification.reproduction_confirmed
                                ? `Confirmed (${investigationResult.verification.pre_fix?.exception_type || "Observed"})`
                                : "Not Reproduced"}
                            </strong>
                          </div>
                          <div className="verification-stat">
                            <span>Fix Verification</span>
                            <strong>
                              {investigationResult.verification.fix_verified
                                ? "Fix Verified"
                                : investigationResult.verification.reproduction_resolved
                                  ? "Reproduction Resolved Only"
                                  : "Fix Unverified"}
                            </strong>
                          </div>
                          <div className="verification-stat">
                            <span>Regression Suite</span>
                            <strong>
                              {investigationResult.verification.regression_suite_passed === true
                                ? "All Passed"
                                : investigationResult.verification.regression_suite_passed === false
                                  ? "Regression Failed"
                                  : "None configured"}
                            </strong>
                          </div>
                        </div>

                        {investigationResult.verification.diff && (
                          <div className="diff-card-container">
                            <details className="history-item" style={{ marginBottom: "10px" }} open>
                              <summary>
                                <strong>Verified Patch Diff ({investigationResult.verification.patched_file})</strong>
                              </summary>
                              <pre className="diff-view">{investigationResult.verification.diff}</pre>
                            </details>

                            {investigationResult.verification.patched_content && (
                              <div className="patch-approval-card">
                                <div className="patch-approval-header">
                                  <div>
                                    <div className="patch-badge-row">
                                      <span className="patch-badge">Verified Minimal Fix</span>
                                      <span className="patch-target-tag">{investigationResult.verification.patched_file}</span>
                                    </div>
                                    <p className="muted tiny" style={{ margin: "5px 0 0" }}>
                                      Review the diff above. Tap below to approve and edit this file in your codebase directly from your phone.
                                    </p>
                                  </div>
                                </div>

                                {patchApplyResult && (
                                  <div className="patch-applied-banner" role="status">
                                    <span className="patch-applied-icon">✓</span>
                                    <div>
                                      <strong>Fix Applied to Codebase!</strong>
                                      <p className="tiny" style={{ margin: "2px 0 0" }}>
                                        {patchApplyResult.message}
                                        {patchApplyResult.backupFile && ` (Safety backup created: ${patchApplyResult.backupFile})`}
                                      </p>
                                    </div>
                                  </div>
                                )}

                                {patchApplyError && (
                                  <div className="error-message" role="alert" style={{ margin: "8px 0" }}>
                                    <strong>Could not apply patch:</strong> {patchApplyError}
                                  </div>
                                )}

                                {!patchApplyResult && (
                                  <button
                                    type="button"
                                    className="apply-patch-button"
                                    disabled={applyingPatch}
                                    onClick={() =>
                                      handleApplyPatch(
                                        investigationResult.repository_id || repoId,
                                        investigationResult.verification.patched_file,
                                        investigationResult.verification.patched_content
                                      )
                                    }
                                  >
                                    {applyingPatch ? (
                                      <span>Applying fix to codebase...</span>
                                    ) : (
                                      <>
                                        <span>✓ Approve & Apply Fix to Codebase</span>
                                        <span className="button-arrow">↗</span>
                                      </>
                                    )}
                                  </button>
                                )}
                              </div>
                            )}
                          </div>
                        )}

                        {investigationResult.verification.test_code && (
                          <details className="history-item">
                            <summary>
                              <strong>Reproduction Test Script</strong>
                            </summary>
                            <pre className="diff-view">{investigationResult.verification.test_code}</pre>
                          </details>
                        )}
                      </section>
                    )}

                    {investigationResult.screenshot_analysis && (
                      <section className="screenshot-card">
                        <div className="screenshot-header">
                          <span className="screenshot-badge-icon">📸</span>
                          <div>
                            <strong>Screenshot Diagnostics & Extracted Error</strong>
                            <p className="muted tiny" style={{ margin: "2px 0 0" }}>
                              Multimodal extraction of error traces, code context, and diagnostics from your image
                            </p>
                          </div>
                        </div>
                        <div className="screenshot-content markdown-body">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>
                            {investigationResult.screenshot_analysis}
                          </ReactMarkdown>
                        </div>
                      </section>
                    )}

                    <article className="panel result-panel">
                      <h3>Analysis</h3>

                      {/* Render the investigation report as formatted Markdown. */}
                      <div className="investigation-answer">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {investigationResult.answer}
                        </ReactMarkdown>
                      </div>
                    </article>

                    <div className="section-heading">
                      <div>
                        <p className="eyebrow">SOURCE EVIDENCE</p>
                        <h2>Tool activity</h2>
                      </div>
                    </div>

                    {Array.isArray(investigationResult.evidence) &&
                    investigationResult.evidence.length > 0 ? (
                      <div className="history-list">
                        {investigationResult.evidence.map((item, index) => (
                          <details className="history-item" key={`${item.tool}-${index}`}>
                            <summary>
                              <div className="history-title">
                                <strong>{item.tool}</strong>
                                <small>
                                  {formatStatus(item.status)}
                                </small>
                              </div>
                            </summary>

                            <pre className="investigation-answer">
                              {item.result}
                            </pre>
                          </details>
                        ))}
                      </div>
                    ) : (
                      <p className="muted">
                        The agent did not return tool evidence.
                      </p>
                    )}

                    <p className="muted tiny">
                      {investigationResult.verification?.valid
                        ? "Empirically verified in an isolated shadow sandbox. No host files were modified."
                        : "These are model-generated findings based on returned evidence."}
                    </p>
                  </section>
                )}
              </section>

              <section className="section-block">
                <div className="section-heading">
                  <div>
                    <p className="eyebrow">WORK HISTORY</p>
                    <h2>Recent investigations</h2>
                  </div>

                  <button
                    className="text-button"
                    onClick={() => setPage("Investigations")}
                  >
                    View all →
                  </button>
                </div>

                {historyLoading && (
                  <p className="muted" role="status">Loading investigation history...</p>
                )}

                {historyError && (
                  <p className="error-message" role="alert">
                    Could not load investigation history: {historyError}
                  </p>
                )}

                {!historyLoading && !historyError && recentInvestigations.length === 0 && (
                  <p className="muted">No investigations saved yet. Run your first investigation above.</p>
                )}

                <div className="history-list">
                  {recentInvestigations.slice(0, 5).map((item) => (
                    <article className="history-item" key={item.id}>
                      <div className="history-icon">
                        {item.status === "completed" ? "✓" : item.status === "failed" ? "✗" : "↗"}
                      </div>
                      <div className="history-title">
                        <strong>{historyTitle(item)}</strong>
                        <small>
                          {item.repository_name || item.repository_id} · {formatHistoryTime(item.created_at)}
                        </small>
                      </div>
                      <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                        {renderVerificationBadge(item.verification)}
                        <span
                          className={`history-status ${
                            item.status === "completed"
                              ? "resolved"
                              : item.status === "failed"
                                ? "failed"
                                : "incomplete"
                          }`}
                        >
                          {historyStatus(item.status)}
                        </span>
                      </div>
                      <span className="history-arrow">→</span>
                    </article>
                  ))}
                </div>
              </section>
            </>
          )}

          {page === "Repositories" && (
            <section className="section-block">
              <p className="eyebrow">PROJECT ACCESS</p>
              <h1>Repositories</h1>
              <p className="subtitle">
                Inspect local repositories or import a public GitHub repository
                into DevPilot-managed local storage.
              </p>

              <form className="github-import-form" onSubmit={handlePublicGitHubImport}>
                <p className="field-label">Import public GitHub repository</p>
                <p className="muted tiny">
                  Clones a public HTTPS repository locally. It does not store GitHub
                  credentials, push changes, or open pull requests.
                </p>
                <label className="field-label" htmlFor="github-repository-id">
                  Repository ID
                </label>
                <input
                  id="github-repository-id"
                  value={githubRepositoryId}
                  onChange={(event) => setGithubRepositoryId(event.target.value)}
                  placeholder="example-project"
                  maxLength="100"
                  disabled={githubImporting}
                />
                <label className="field-label" htmlFor="github-repository-name">
                  Display name
                </label>
                <input
                  id="github-repository-name"
                  value={githubRepositoryName}
                  onChange={(event) => setGithubRepositoryName(event.target.value)}
                  placeholder="Example Project"
                  maxLength="200"
                  disabled={githubImporting}
                />
                <label className="field-label" htmlFor="github-repository-url">
                  Public GitHub HTTPS URL
                </label>
                <input
                  id="github-repository-url"
                  type="url"
                  value={githubRepositoryUrl}
                  onChange={(event) => setGithubRepositoryUrl(event.target.value)}
                  placeholder="https://github.com/owner/repository"
                  maxLength="500"
                  disabled={githubImporting}
                />
                {githubImportError && (
                  <p className="error-message" role="alert">{githubImportError}</p>
                )}
                <button className="primary-button" type="submit" disabled={githubImporting}>
                  {githubImporting ? "Importing..." : "Import public GitHub repo"}
                  <span>{githubImporting ? "…" : "↗"}</span>
                </button>
              </form>

              {repositoriesLoading && (
                <p role="status">Loading repositories...</p>
              )}

              {repositoryError && (
                <p className="error-message" role="alert">
                  Could not load repositories: {repositoryError}
                </p>
              )}

              {!repositoriesLoading &&
                !repositoryError &&
                repositories.length === 0 && (
                  <p className="muted">No repositories are registered yet.</p>
                )}

              <div className="repository-list">
                {repositories.map((repo) => (
                  <button
                    key={repo.repository_id}
                    className={`repository-card ${
                      repoId === repo.repository_id ? "selected" : ""
                    }`}
                    disabled={repo.status !== "connected"}
                    onClick={() => {
                      setRepoId(repo.repository_id);
                      setPage("Overview");
                      clearResult();
                    }}
                  >
                    <div className="repository-icon">
                      {repo.source_type?.toLowerCase().includes("github")
                        ? "GH"
                        : "⌘"}
                    </div>

                    <div>
                      <strong>{repo.name}</strong>
                      <small>
                        {repo.source_type} · ID: {repo.repository_id}
                      </small>
                    </div>

                    <span
                      className={`history-status ${
                        repo.status === "connected" ? "resolved" : ""
                      }`}
                    >
                      {formatStatus(repo.status)}
                    </span>
                  </button>
                ))}
              </div>

              <button
                className="text-button"
                type="button"
                onClick={() => window.location.reload()}
              >
                Refresh repository data ↻
              </button>
            </section>
          )}

          {page === "Investigations" && (
            <section className="section-block">
              <p className="eyebrow">INVESTIGATION LOG</p>
              <h1>Investigations</h1>
              <p className="subtitle">
                Review previous bug reports and their outcomes.
              </p>

              <div className="section-heading">
                <div>
                  <p className="muted tiny">
                    {recentInvestigations.length} saved investigation
                    {recentInvestigations.length === 1 ? "" : "s"}
                  </p>
                </div>
                <button
                  className="text-button"
                  type="button"
                  onClick={loadInvestigations}
                  disabled={historyLoading}
                >
                  {historyLoading ? "Refreshing..." : "Refresh history ↻"}
                </button>
              </div>

              {historyError && (
                <p className="error-message" role="alert">
                  Could not load investigation history: {historyError}
                </p>
              )}

              {historyLoading && (
                <p className="muted" role="status">Loading investigation history...</p>
              )}

              {!historyLoading && !historyError && recentInvestigations.length === 0 && (
                <p className="muted">No saved investigations yet.</p>
              )}

              <div className="history-list">
                {recentInvestigations.map((item) => (
                  <details className="history-item" key={item.id}>
                    <summary>
                      <div className="history-title">
                        <strong>{historyTitle(item)}</strong>
                        <small>
                          {item.repository_name || item.repository_id} · {formatHistoryTime(item.created_at)}
                        </small>
                      </div>
                      <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                        {renderVerificationBadge(item.verification)}
                        <span
                          className={`history-status ${
                            item.status === "completed"
                              ? "resolved"
                              : item.status === "failed"
                                ? "failed"
                                : "incomplete"
                          }`}
                        >
                          {historyStatus(item.status)}
                        </span>
                      </div>
                    </summary>

                    <div className="history-detail">
                      <p><strong>Repository:</strong> {item.repository_name || item.repository_id}</p>

                      {item.verification && (
                        <div className="verification-card" style={{ margin: "14px 0" }}>
                          <div className="verification-header">
                            <div className="verification-title">
                              <span>🛡️</span>
                              <strong>Empirical Sandbox Verification</strong>
                            </div>
                            {renderVerificationBadge(item.verification)}
                          </div>
                          <div className="verification-meta">
                            <div className="verification-stat">
                              <span>Baseline Reproduction</span>
                              <strong>
                                {item.verification.reproduction_confirmed
                                  ? `Confirmed (${item.verification.pre_fix?.exception_type || "Observed"})`
                                  : "Not Reproduced"}
                              </strong>
                            </div>
                            <div className="verification-stat">
                              <span>Fix Verification</span>
                              <strong>
                                {item.verification.fix_verified
                                  ? "Fix Verified"
                                  : item.verification.reproduction_resolved
                                    ? "Reproduction Resolved Only"
                                    : "Fix Unverified"}
                              </strong>
                            </div>
                            <div className="verification-stat">
                              <span>Regression Suite</span>
                              <strong>
                                {item.verification.regression_suite_passed === true
                                  ? "Passed"
                                  : item.verification.regression_suite_passed === false
                                    ? "Regression Failed"
                                    : "None configured"}
                              </strong>
                            </div>
                          </div>
                          {item.verification.diff && (
                            <div className="diff-card-container">
                              <pre className="diff-view">{item.verification.diff}</pre>
                              {item.verification.patched_content && (
                                <div className="patch-approval-card mini">
                                  <button
                                    type="button"
                                    className="apply-patch-button secondary"
                                    disabled={applyingPatch}
                                    onClick={() =>
                                      handleApplyPatch(
                                        item.repository_id,
                                        item.verification.patched_file,
                                        item.verification.patched_content
                                      )
                                    }
                                  >
                                    {applyingPatch ? "Applying fix..." : `✓ Approve & Apply Fix to ${item.verification.patched_file}`}
                                  </button>
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )}

                      <p><strong>Report:</strong></p>
                      <div className="investigation-answer">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {item.answer || "No report text was saved."}
                        </ReactMarkdown>
                      </div>

                      <p><strong>Source evidence</strong></p>
                      {Array.isArray(item.evidence) && item.evidence.length > 0 ? (
                        item.evidence.map((evidence, index) => (
                          <details className="history-item" key={`${item.id}-${evidence.tool}-${index}`}>
                            <summary>
                              {evidence.tool} · {formatStatus(evidence.status)}
                            </summary>
                            <pre className="investigation-answer">{evidence.result}</pre>
                          </details>
                        ))
                      ) : (
                        <p className="muted">No evidence was saved for this report.</p>
                      )}
                    </div>
                  </details>
                ))}
              </div>
            </section>
          )}

          {page === "Durable Tasks" && (
            <section className="section-block">
              <p className="eyebrow">POSTGRESQL TASK LIFECYCLE</p>
              <h1>Durable Tasks</h1>
              <p className="subtitle">
                Inspect durable tasks received via GitHub webhooks or API, view audit trails, and trigger state transitions.
              </p>

              <form
                className="task-search-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  loadDurableTask();
                }}
              >
                <input
                  className="task-search-input"
                  type="text"
                  placeholder="Enter Task UUID (e.g. b16305f2-0ce0-42a7-919d-d019b196e724)"
                  value={taskIdInput}
                  onChange={(e) => {
                    setTaskIdInput(e.target.value);
                    setTaskError("");
                    setTaskSuccessMessage("");
                  }}
                  disabled={taskLoading}
                />
                <button className="primary-button" type="submit" disabled={taskLoading || !taskIdInput.trim()}>
                  {taskLoading ? "Inspecting..." : "Inspect Task 🔍"}
                </button>
              </form>

              {taskError && (
                <p className="error-message" role="alert">
                  {taskError}
                </p>
              )}

              {taskSuccessMessage && (
                <div style={{ background: "rgba(46, 160, 67, 0.15)", color: "#3fb950", border: "1px solid rgba(63, 185, 80, 0.3)", padding: "0.75rem 1rem", borderRadius: "8px", marginBottom: "1.5rem" }}>
                  ✓ {taskSuccessMessage}
                </div>
              )}

              {durableTask && (
                <div className="task-card-container">
                  <div className="task-header-row">
                    <div>
                      <small style={{ color: "#8b949e", textTransform: "uppercase", letterSpacing: "0.05em", fontSize: "0.75rem" }}>
                        TASK ID
                      </small>
                      <h2 style={{ margin: "0.25rem 0 0 0", fontFamily: "Consolas, monospace", fontSize: "1.2rem", color: "#f0f6fc" }}>
                        {durableTask.task_id}
                      </h2>
                    </div>

                    <div style={{ display: "flex", gap: "10px", alignItems: "center" }}>
                      <span className={`status-badge ${durableTask.state}`}>
                        {durableTask.state.replace("_", " ")}
                      </span>
                      {durableTask.state !== "awaiting_approval" && durableTask.state !== "failed" && durableTask.state !== "cancelled" && (
                        <button
                          className="primary-button"
                          type="button"
                          onClick={() => handleProcessDurableTask(durableTask.task_id)}
                          disabled={taskProcessing}
                          style={{ padding: "0.45rem 1rem", fontSize: "0.85rem" }}
                        >
                          {taskProcessing ? "Advancing..." : "Process Step ⚡"}
                        </button>
                      )}
                    </div>
                  </div>

                  <div className="task-details-grid">
                    <div className="task-detail-item">
                      <small>Source</small>
                      <strong>{durableTask.source}</strong>
                    </div>
                    <div className="task-detail-item">
                      <small>Repository Node ID</small>
                      <strong>{durableTask.repository_node_id}</strong>
                    </div>
                    <div className="task-detail-item">
                      <small>Base SHA</small>
                      <strong>{durableTask.base_sha ? durableTask.base_sha.slice(0, 10) : "N/A"}</strong>
                    </div>
                    <div className="task-detail-item">
                      <small>Created</small>
                      <strong>{formatHistoryTime(durableTask.created_at)}</strong>
                    </div>
                  </div>

                  <div>
                    <h3 style={{ fontSize: "0.95rem", color: "#c9d1d9", marginBottom: "0.5rem" }}>
                      Lifecycle Progression
                    </h3>
                    <div className="task-lifecycle-stepper">
                      {TASK_LIFECYCLE_STAGES.map((stage, idx) => {
                        const status = getStepStatus(stage.key, durableTask.state);
                        return (
                          <div key={stage.key} style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                            <div className={`step-pill ${status}`}>
                              <span>
                                {status === "completed" ? "✓" : status === "active" ? "●" : status === "failed" ? "✕" : "○"}
                              </span>
                              <span>{stage.label}</span>
                            </div>
                            {idx < TASK_LIFECYCLE_STAGES.length - 1 && <span className="step-arrow">→</span>}
                          </div>
                        );
                      })}
                    </div>
                  </div>

                  <div className="audit-events-section">
                    <h3 style={{ fontSize: "0.95rem", color: "#c9d1d9", marginBottom: "0.5rem" }}>
                      Audit Event Trail ({taskAuditEvents.length})
                    </h3>
                    {taskAuditEvents.length === 0 ? (
                      <p className="muted">No audit events recorded yet.</p>
                    ) : (
                      <div className="audit-timeline">
                        {taskAuditEvents.map((evt) => (
                          <div className="audit-event-item" key={evt.event_id}>
                            <span className="audit-event-name">{evt.event_type}</span>
                            <span className="audit-event-time">{formatHistoryTime(evt.created_at)}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )}

              {!durableTask && !taskLoading && (
                <div style={{ background: "rgba(22, 27, 34, 0.5)", border: "1px dashed rgba(140, 150, 170, 0.25)", borderRadius: "10px", padding: "2rem", textAlign: "center", color: "#8b949e", marginTop: "1rem" }}>
                  <p style={{ margin: "0 0 0.5rem 0", fontSize: "1rem", color: "#c9d1d9" }}>
                    No task loaded
                  </p>
                  <p style={{ margin: 0, fontSize: "0.85rem" }}>
                    Paste a Task UUID above to inspect its durable state and audit log, or trigger inbound events via <code>POST /api/github/webhooks</code>.
                  </p>
                </div>
              )}
            </section>
          )}
        </div>
      </main>
    </div>
  );
}

export default App;
