# Optional: global external HTTPS load balancer + Cloud Armor in front of the gateway (serverless NEG).
# DNS: point var.domain at the output `lb_ip`. The managed certificate provisions after DNS resolves.
resource "google_compute_region_network_endpoint_group" "gw" {
  count                 = var.enable_lb ? 1 : 0
  name                  = "${local.prefix}-gw-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run { service = google_cloud_run_v2_service.svc["gateway"].name }
}

resource "google_compute_security_policy" "armor" {
  count = var.enable_lb ? 1 : 0
  name  = "${local.prefix}-armor"
  rule {
    action   = "throttle"
    priority = 1000
    match {
      versioned_expr = "SRC_IPS_V1"
      config { src_ip_ranges = ["*"] }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 600
        interval_sec = 60
      }
    }
    description = "per-IP rate limit"
  }
  rule {
    action   = "deny(403)"
    priority = 2000
    match {
      expr { expression = "evaluatePreconfiguredWaf('sqli-v33-stable') || evaluatePreconfiguredWaf('xss-v33-stable')" }
    }
    description = "OWASP SQLi/XSS"
  }
  rule {
    action   = "allow"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config { src_ip_ranges = ["*"] }
    }
    description = "default allow"
  }
}

resource "google_compute_backend_service" "gw" {
  count                 = var.enable_lb ? 1 : 0
  name                  = "${local.prefix}-gw-backend"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.armor[0].id
  backend { group = google_compute_region_network_endpoint_group.gw[0].id }
  log_config {
    enable      = true
    sample_rate = 0.1
  }
}
resource "google_compute_url_map" "gw" {
  count           = var.enable_lb ? 1 : 0
  name            = "${local.prefix}-gw"
  default_service = google_compute_backend_service.gw[0].id
}
resource "google_compute_managed_ssl_certificate" "gw" {
  count = var.enable_lb ? 1 : 0
  name  = "${local.prefix}-gw-cert"
  managed { domains = [var.domain] }
}
resource "google_compute_target_https_proxy" "gw" {
  count            = var.enable_lb ? 1 : 0
  name             = "${local.prefix}-gw-proxy"
  url_map          = google_compute_url_map.gw[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.gw[0].id]
}
resource "google_compute_global_address" "gw" {
  count = var.enable_lb ? 1 : 0
  name  = "${local.prefix}-gw-ip"
}
resource "google_compute_global_forwarding_rule" "gw" {
  count                 = var.enable_lb ? 1 : 0
  name                  = "${local.prefix}-gw-fr"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_https_proxy.gw[0].id
  port_range            = "443"
  ip_address            = google_compute_global_address.gw[0].id
}
