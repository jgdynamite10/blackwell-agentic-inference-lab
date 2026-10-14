#!/bin/bash
# First-boot deadline. A later driver reboot must not extend it.
set -euo pipefail
install -d -m 0700 /var/lib/bwlab
if [ ! -f /var/lib/bwlab/ttl-deadline ]; then
  date -u -d '+10 hours' +%s > /var/lib/bwlab/ttl-deadline
fi
cat > /usr/local/sbin/bwlab-ttl <<'EOF'
#!/bin/bash
set -euo pipefail
deadline="$(cat /var/lib/bwlab/ttl-deadline)"
now="$(date -u +%s)"
if [ "${now}" -ge "${deadline}" ]; then
  /sbin/shutdown -h now
fi
EOF
chmod 0755 /usr/local/sbin/bwlab-ttl
cat > /etc/systemd/system/bwlab-ttl.service <<'EOF'
[Unit]
Description=Terminate this D-0041 host at its first-boot deadline
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/bwlab-ttl
EOF
cat > /etc/systemd/system/bwlab-ttl.timer <<'EOF'
[Unit]
Description=Check the D-0041 teardown deadline
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=15s
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now bwlab-ttl.timer
systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service 2>/dev/null || systemctl enable --now amazon-ssm-agent 2>/dev/null || true
