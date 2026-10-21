document.addEventListener("alpine:init", () => {
  Alpine.data("accountMenu", () => ({
    open: false,
    passwordOpen: false,
    passwordForm: { current_password: "", new_password: "", repeat: "" },
    passwordError: "",
    passwordNotice: "",

    async changePassword() {
      this.passwordError = "";
      this.passwordNotice = "";
      if (this.passwordForm.new_password !== this.passwordForm.repeat) {
        this.passwordError = "Mật khẩu nhập lại không khớp.";
        return;
      }

      try {
        const token = localStorage.getItem("nc_token");
        const response = await fetch("/api/auth/change-password", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({
            current_password: this.passwordForm.current_password,
            new_password: this.passwordForm.new_password,
          }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          const details = data.details && Object.values(data.details).flat().join(" ");
          throw new Error([data.message || `Request failed (${response.status})`, details].filter(Boolean).join(" "));
        }

        localStorage.setItem("nc_token", data.access_token);
        localStorage.setItem("nc_user", JSON.stringify(data.user));
        window.dispatchEvent(new CustomEvent("account-session-updated", {
          detail: { access_token: data.access_token, user: data.user },
        }));
        this.passwordForm = { current_password: "", new_password: "", repeat: "" };
        this.passwordNotice = "Đã đổi mật khẩu thành công.";
      } catch (error) {
        this.passwordError = error.message || "Không thể đổi mật khẩu.";
      }
    },
  }));
});
