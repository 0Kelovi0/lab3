import subprocess
import argparse
import json
import logging
import time

logging.basicConfig(level=logging.INFO, format='-- %(message)s --')

def run_cmd(cmd, input_text=None, capture=False, check=True):
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=capture,
        check=check
    )

def launch_vms(size, cpu, ram, disk):
    logging.info(f"Launching {size} VMs ({cpu} CPUs, {ram}MB RAM, {disk}G disk each)...")
    for i in range(size):
        start_time = time.time()
        vm_name = "manager" if i == 0 else f"worker{i}"
        run_cmd([
            "multipass", "launch", "24.04",
            "-n", vm_name,
            "--cpus", str(cpu),
            "--memory", f"{ram}M",
            "--disk", f"{disk}G"
        ])
        elapsed = time.time() - start_time
        logging.info(f"VM {vm_name} launched in {elapsed:.2f}s")

def install_packages(size):
    pkgs = (
        "nfs-kernel-server nfs-common rpcbind "
        "build-essential openmpi-bin libopenmpi-dev"
    )
    for i in range(size):
        vm_name = "manager" if i == 0 else f"worker{i}"
        logging.info(f"Waiting for cloud-init and installing OpenMPI/NFS on {vm_name}...")
        run_cmd(["multipass", "exec", vm_name, "--", "cloud-init", "status", "--wait"], check=False)
        run_cmd([
            "multipass", "exec", vm_name, "--", "sudo", "bash", "-c",
            f"DEBIAN_FRONTEND=noninteractive apt-get update && apt-get install -y --no-install-recommends {pkgs}"
        ])

def get_vm_ips():
    logging.info("Fetching VM IP addresses...")
    res = run_cmd(["multipass", "list", "--format=json"], capture=True)
    vm_info = json.loads(res.stdout)["list"]
    vm_info.sort(key=lambda vm: vm["name"])
    vm_ips = {vm["name"]: vm["ipv4"][0] for vm in vm_info if vm["state"] == "Running"}
    logging.info(f"VM IPs: {vm_ips}")
    return vm_ips

def update_etc_hosts(vm_ips):
    for name in vm_ips:
        logging.info(f"Updating /etc/hosts on {name}...")
        lines = "\n".join(f"{ip} {other}" for other, ip in vm_ips.items() if other != name) + "\n"
        run_cmd(
            ["multipass", "exec", name, "--", "sudo", "tee", "-a", "/etc/hosts", "/etc/cloud/templates/hosts.debian.tmpl"],
            input_text=lines,
            capture=True
        )

def setup_ssh(size):
    vms = ["manager" if i == 0 else f"worker{i}" for i in range(size)]
    pub_keys = {}

    for vm in vms:
        logging.info(f"Generating SSH key on {vm}...")
        run_cmd([
            "multipass", "exec", vm, "--", "bash", "-c",
            "test -f ~/.ssh/id_rsa || ssh-keygen -q -t rsa -N '' -f ~/.ssh/id_rsa"
        ])
        res = run_cmd(["multipass", "exec", vm, "--", "cat", "/home/ubuntu/.ssh/id_rsa.pub"], capture=True)
        pub_keys[vm] = res.stdout.strip()

    for target in vms:
        logging.info(f"Configuring authorized_keys on {target}...")
        other_keys = "\n".join(key for src, key in pub_keys.items() if src != target) + "\n"
        run_cmd(
            ["multipass", "exec", target, "--", "tee", "-a", "/home/ubuntu/.ssh/authorized_keys"],
            input_text=other_keys,
            capture=True
        )

    for src in vms:
        for dst in vms:
            if src != dst:
                run_cmd([
                    "multipass", "exec", src, "--", "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=10",
                    dst, "true"
                ])
    logging.info("Passwordless SSH configured between all nodes.")

def setup_nfs(size):
    logging.info("Configuring NFS server on manager...")
    run_cmd(["multipass", "exec", "manager", "--", "mkdir", "-p", "/home/ubuntu/cloud"])
    run_cmd(
        ["multipass", "exec", "manager", "--", "sudo", "tee", "/etc/exports"],
        input_text="/home/ubuntu/cloud *(rw,sync,no_root_squash,no_subtree_check)\n",
        capture=True
    )
    run_cmd(["multipass", "exec", "manager", "--", "sudo", "systemctl", "enable", "--now", "rpcbind", "nfs-kernel-server"])
    run_cmd(["multipass", "exec", "manager", "--", "sudo", "exportfs", "-ra"])

    for i in range(1, size):
        worker = f"worker{i}"
        logging.info(f"Mounting NFS share on {worker}...")
        run_cmd(["multipass", "exec", worker, "--", "mkdir", "-p", "/home/ubuntu/cloud"])
        run_cmd(["multipass", "exec", worker, "--", "sudo", "systemctl", "enable", "--now", "rpcbind"])
        run_cmd(
            ["multipass", "exec", worker, "--", "sudo", "tee", "-a", "/etc/fstab"],
            input_text="manager:/home/ubuntu/cloud /home/ubuntu/cloud nfs defaults,_netdev 0 0\n",
            capture=True
        )
        run_cmd(["multipass", "exec", worker, "--", "sudo", "mount", "-a"])

def create_mpi_hosts(size, cpu):
    logging.info("Creating /home/ubuntu/cloud/mpi_hosts...")
    vms = ["manager" if i == 0 else f"worker{i}" for i in range(size)]
    content = "\n".join(f"{vm} slots={cpu}" for vm in vms) + "\n"
    run_cmd(
        ["multipass", "exec", "manager", "--", "tee", "/home/ubuntu/cloud/mpi_hosts"],
        input_text=content,
        capture=True
    )

def main():
    parser = argparse.ArgumentParser(description='Setup VMs with NFS and OpenMPI')
    parser.add_argument('size', metavar='S', type=int, help='Number of VMs in the cluster')
    parser.add_argument('--cpu', type=int, default=1, help='Number of CPUs per VM (default: 1)')
    parser.add_argument('--ram', type=int, default=1024, help='RAM in MB per VM (default: 1024)')
    parser.add_argument('--disk', type=int, default=10, help='Disk size in GB per VM (default: 10)')
    args = parser.parse_args()

    launch_vms(args.size, args.cpu, args.ram, args.disk)
    install_packages(args.size)
    vm_ips = get_vm_ips()
    update_etc_hosts(vm_ips)
    setup_ssh(args.size)
    setup_nfs(args.size)
    create_mpi_hosts(args.size, args.cpu)
    logging.info("Cluster setup finished successfully!")

if __name__ == "__main__":
    main()