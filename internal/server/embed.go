package server

import _ "embed"

// Dashboard + landing HTML are baked into the binary (the Python original
// read dashboard.html from disk at import and held the landing page as a
// module string). embed.FS-equivalent via go:embed keeps zero runtime files.

//go:embed templates/dashboard.html
var DashboardHTML string

//go:embed templates/landing.html
var LandingHTML string
