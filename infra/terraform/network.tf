# Internal Cloud Run services use INTERNAL ingress; callers reach them through the VPC (direct VPC egress) so the only public
# surface is the gateway. Private Google Access lets the VPC reach *.run.app / Google APIs; NAT is only for non-Google egress.
resource "google_compute_network" "vpc" {
  name                    = "${local.prefix}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}
resource "google_compute_subnetwork" "run" {
  name                     = "${local.prefix}-run"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = "10.20.0.0/22"
  private_ip_google_access = true
}
resource "google_compute_router" "r" {
  count   = var.enable_nat ? 1 : 0
  name    = "${local.prefix}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}
resource "google_compute_router_nat" "nat" {
  count                              = var.enable_nat ? 1 : 0
  name                               = "${local.prefix}-nat"
  router                             = google_compute_router.r[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}
