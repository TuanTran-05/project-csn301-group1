(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root.document) {
    root.document.addEventListener("DOMContentLoaded", function () {
      api.mount(root.document, root.fetch.bind(root));
    });
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
  const API_PATH = "/api/lab-bootstrap-demo/analyze-archive";
  const EXPORT_PATH = "/api/lab-bootstrap-demo/export-inspection";
  const REPORT_FILENAME = "lab-bootstrap-inspection-report.zip";
  const CSRF_HEADER = "X-CSRF-TOKEN";

  function readCookie(cookieHeader, cookieName) {
    if (!cookieName) return "";
    for (const part of String(cookieHeader || "").split(";")) {
      const separator = part.indexOf("=");
      if (separator < 0) continue;
      const name = part.slice(0, separator).trim();
      if (name === cookieName) {
        return decodeURIComponent(part.slice(separator + 1).trim());
      }
    }
    return "";
  }

  function toViewModel(data) {
    if (!data || typeof data !== "object" || !data.analysis || !data.inspection) {
      throw new Error("Phản hồi phân tích thiếu dữ liệu bắt buộc.");
    }
    const analysis = data.analysis;
    const inspection = data.inspection;
    if (
      data.status !== "ANALYZED_UNVERIFIED_SCHEMA" ||
      analysis.status !== data.status ||
      typeof analysis.status !== "string" ||
      analysis.preview_compatible !== false ||
      data.preview_compatible !== false ||
      !Array.isArray(analysis.warnings) ||
      !Array.isArray(analysis.nodes) ||
      inspection.planner_eligible !== false ||
      data.planner_eligible !== false ||
      data.planner_status !== "NOT_RUN" ||
      data.device_actions !== "NOT_PERFORMED" ||
      !Array.isArray(inspection.segments) ||
      !Array.isArray(inspection.blockers) ||
      !Array.isArray(inspection.issues) ||
      !Array.isArray(data.planner_blockers) ||
      data.gateway_status !== "NOT_SUPPORTED"
    ) {
      throw new Error("Phản hồi phân tích không đúng schema Safe Demo.");
    }
    return {
      status: analysis.status,
      previewCompatible: analysis.preview_compatible,
      plannerEligible: inspection.planner_eligible,
      plannerStatus: typeof data.planner_status === "string" ? data.planner_status : "UNKNOWN",
      gatewayStatus: data.gateway_status,
      deviceActions: typeof data.device_actions === "string" ? data.device_actions : "UNKNOWN",
      archiveSha256: typeof analysis.source_archive_sha256 === "string"
        ? analysis.source_archive_sha256 : "Không có checksum archive",
      nodes: analysis.nodes.map(function (node) {
        if (!node || typeof node !== "object") return "Node record không hợp lệ.";
        const interfaces = Array.isArray(node.interfaces)
          ? node.interfaces.map(function (item) {
            return String(item.id) + ": " + (item.name || "tên interface chưa có");
          }).join(", ")
          : "metadata interface chưa có";
        return "ID " + String(node.id) + " — " + String(node.name || "chưa có tên") +
          " — template: " + String(node.template || "chưa khai báo") +
          " — image: " + String(node.image || "chưa khai báo") +
          " — image_build: " + String(node.image_build || "chưa khai báo; không suy diễn") +
          " — interfaces: " + interfaces;
      }),
      segments: inspection.segments.map(function (segment) {
        if (!segment || typeof segment !== "object") return "Segment record không hợp lệ.";
        const endpoints = Array.isArray(segment.endpoints)
          ? segment.endpoints.map(function (endpoint) {
            return String(endpoint.node_id) + ":" + String(endpoint.interface || endpoint.interface_label || "interface chưa xác minh");
          }).join(" ↔ ")
          : "endpoint chưa có";
        return String(segment.id) + " [" + String(segment.kind || "unknown") + "] — " + endpoints;
      }),
      warnings: analysis.warnings,
      issues: inspection.issues,
      plannerBlockers: data.planner_blockers
    };
  }

  function appendTextItems(document, list, items) {
    list.replaceChildren();
    items.forEach(function (item) {
      const row = document.createElement("li");
      row.textContent = String(item);
      list.appendChild(row);
    });
    if (!items.length) {
      const row = document.createElement("li");
      row.textContent = "Không có mục nào.";
      list.appendChild(row);
    }
  }

  function renderResult(rootElement, data) {
    const model = toViewModel(data);
    const document = rootElement.ownerDocument;
    rootElement.querySelector("#analysis-results").hidden = false;
    rootElement.querySelector("#analysis-status").textContent = model.status;
    rootElement.querySelector("#preview-compatible").textContent = model.previewCompatible ? "true" : "false";
    rootElement.querySelector("#planner-status").textContent =
      model.plannerEligible
        ? model.plannerStatus + " — đủ điều kiện review, nhưng endpoint không tạo plan"
        : model.plannerStatus + " — planner bị khóa";
    rootElement.querySelector("#gateway-status").textContent = model.gatewayStatus;
    rootElement.querySelector("#device-actions").textContent = model.deviceActions;
    rootElement.querySelector("#archive-sha256 code").textContent = model.archiveSha256;
    appendTextItems(document, rootElement.querySelector("#nodes"), model.nodes);
    appendTextItems(document, rootElement.querySelector("#segments"), model.segments);
    appendTextItems(document, rootElement.querySelector("#warnings"), model.warnings);
    appendTextItems(document, rootElement.querySelector("#issues"), model.issues);
    appendTextItems(document, rootElement.querySelector("#planner-blockers"), model.plannerBlockers);
    const exportButton = rootElement.querySelector("#export-inspection-button");
    if (exportButton) {
      exportButton.hidden = model.issues.length > 0;
      exportButton.disabled = model.issues.length > 0;
    }
    const exportStatus = rootElement.querySelector("#export-status");
    if (exportStatus) {
      exportStatus.textContent = model.issues.length
        ? "Inspection có issue; không thể tạo báo cáo review-only."
        : "Có thể tải báo cáo review-only gồm checksum và số lượng bản ghi; không có IP plan hay cấu hình.";
    }
    return model;
  }

  function mount(document, fetchImpl, downloadImpl) {
    const rootElement = document.querySelector("#lab-bootstrap-demo");
    if (!rootElement) return;
    const cookieAuth = rootElement.dataset.authMode === "cookie";
    const form = rootElement.querySelector("#archive-form");
    const fileInput = rootElement.querySelector("#archive-file");
    const tokenInput = cookieAuth ? null : rootElement.querySelector("#access-token");
    const button = rootElement.querySelector("#analyze-button");
    const status = rootElement.querySelector("#request-status");
    const results = rootElement.querySelector("#analysis-results");
    const exportButton = rootElement.querySelector("#export-inspection-button");
    const exportStatus = rootElement.querySelector("#export-status");
    let inspectedFile = null;

    if (exportButton) {
      exportButton.hidden = true;
      exportButton.disabled = true;
      exportButton.addEventListener("click", async function (event) {
        if (event && typeof event.preventDefault === "function") event.preventDefault();
        if (!inspectedFile) {
          if (exportStatus) exportStatus.textContent = "Hãy phân tích ZIP hợp lệ trước.";
          return;
        }
        const exportFile = inspectedFile;
        const headers = { "Content-Type": "application/zip", "Accept": "application/zip" };
        let credentials = "omit";
        if (cookieAuth) {
          const csrfToken = readCookie(document.cookie, rootElement.dataset.csrfCookieName);
          if (!csrfToken) {
            if (exportStatus) exportStatus.textContent = "Thiếu mã CSRF cục bộ. Hãy tải lại trang Safe Demo.";
            return;
          }
          headers[CSRF_HEADER] = csrfToken;
          credentials = "same-origin";
        } else {
          const token = tokenInput && tokenInput.value.trim();
          if (!token) {
            if (exportStatus) exportStatus.textContent = "Cần access token hợp lệ để xuất báo cáo.";
            return;
          }
          headers.Authorization = "Bearer " + token;
        }

        exportButton.disabled = true;
        if (exportStatus) exportStatus.textContent = "Đang tạo báo cáo inspection review-only…";
        try {
          const response = await fetchImpl(EXPORT_PATH, {
            method: "POST",
            headers: headers,
            credentials: credentials,
            cache: "no-store",
            body: exportFile
          });
          if (!response.ok) {
            const data = await response.json().catch(function () { return {}; });
            throw new Error(data.message || "Không thể tạo báo cáo review-only.");
          }
          if (!fileInput.files || fileInput.files[0] !== exportFile) {
            throw new Error("File đã đổi trong khi tạo báo cáo; hãy phân tích lại trước khi tải.");
          }
          const contentType = response.headers && response.headers.get("Content-Type");
          if (!contentType || !contentType.toLowerCase().includes("application/zip")) {
            throw new Error("Phản hồi export không phải ZIP báo cáo hợp lệ.");
          }
          const blob = await response.blob();
          if (typeof downloadImpl === "function") {
            await downloadImpl(blob, REPORT_FILENAME);
          } else {
            const urlApi = document.defaultView && document.defaultView.URL
              ? document.defaultView.URL
              : (typeof URL !== "undefined" ? URL : null);
            if (!urlApi || typeof urlApi.createObjectURL !== "function") {
              throw new Error("Trình duyệt không hỗ trợ tải báo cáo này.");
            }
            const objectUrl = urlApi.createObjectURL(blob);
            try {
              const link = document.createElement("a");
              link.href = objectUrl;
              link.download = REPORT_FILENAME;
              if (document.body) document.body.appendChild(link);
              link.click();
              if (typeof link.remove === "function") link.remove();
            } finally {
              urlApi.revokeObjectURL(objectUrl);
            }
          }
          if (exportStatus) {
            exportStatus.textContent = "Đã tải báo cáo review-only; đây không phải IP plan, gói import hoặc cấu hình thiết bị.";
          }
        } catch (error) {
          if (exportStatus) {
            exportStatus.textContent = error && error.message
              ? error.message : "Không thể tạo báo cáo review-only.";
          }
        } finally {
          exportButton.disabled = false;
        }
      });
    }

    fileInput.addEventListener("change", function () {
      inspectedFile = null;
      results.hidden = true;
      if (exportButton) {
        exportButton.hidden = true;
        exportButton.disabled = true;
      }
      if (exportStatus) exportStatus.textContent = "";
      status.className = "";
      status.textContent = "File đã đổi; hãy phân tích lại trước khi tải báo cáo.";
    });

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const file = fileInput.files && fileInput.files[0];
      if (!file) {
        status.textContent = "Chọn một file ZIP trước khi phân tích.";
        status.className = "error";
        results.hidden = true;
        return;
      }
      const analyzedFile = file;
      if (file.size > MAX_UPLOAD_BYTES) {
        status.textContent = "ZIP vượt giới hạn 10 MiB.";
        status.className = "error";
        results.hidden = true;
        return;
      }
      const headers = {
        "Content-Type": "application/zip",
        "Accept": "application/json"
      };
      let credentials = "omit";
      if (cookieAuth) {
        const csrfToken = readCookie(
          document.cookie,
          rootElement.dataset.csrfCookieName
        );
        if (!csrfToken) {
          status.textContent = "Thiếu mã CSRF cục bộ. Hãy tải lại trang Safe Demo.";
          status.className = "error";
          results.hidden = true;
          return;
        }
        headers[CSRF_HEADER] = csrfToken;
        credentials = "same-origin";
      } else {
        const token = tokenInput && tokenInput.value.trim();
        if (!token) {
          status.textContent = "Cần dán access token hợp lệ trước khi phân tích.";
          status.className = "error";
          results.hidden = true;
          return;
        }
        headers.Authorization = "Bearer " + token;
      }

      button.disabled = true;
      inspectedFile = null;
      if (exportButton) {
        exportButton.hidden = true;
        exportButton.disabled = true;
      }
      if (exportStatus) exportStatus.textContent = "";
      status.className = "";
      status.textContent = "Đang phân tích archive ngoại tuyến…";
      results.hidden = true;
      try {
        const response = await fetchImpl(API_PATH, {
          method: "POST",
          headers: headers,
          credentials: credentials,
          body: file
        });
        const data = await response.json().catch(function () { return {}; });
        if (!response.ok) {
          const errorPrefix = {
            unsupported_schema: "Unsupported PNETLab schema: ",
            incomplete_topology: "Topology is missing required data: ",
            invalid_archive: "Invalid archive: "
          }[data.error] || "";
          throw new Error(errorPrefix + (data.message || "Không thể phân tích ZIP."));
        }
        if (!fileInput.files || fileInput.files[0] !== analyzedFile) {
          throw new Error("File đã đổi trong khi phân tích; hãy gửi lại file đang được chọn.");
        }
        const viewModel = renderResult(rootElement, data);
        if (viewModel.issues.length === 0) inspectedFile = file;
        status.textContent = "Hoàn tất inspection ngoại tuyến; chưa tạo IP plan hoặc cấu hình.";
      } catch (error) {
        status.textContent = error && error.message ? error.message : "Không thể phân tích ZIP.";
        status.className = "error";
        results.hidden = true;
      } finally {
        button.disabled = false;
      }
    });
  }

  return { toViewModel: toViewModel, renderResult: renderResult, mount: mount };
});
