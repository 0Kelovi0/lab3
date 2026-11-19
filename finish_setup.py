import subprocess
import json
import base64
import logging

logging.basicConfig(level=logging.INFO, format='-- %(message)s --')

def run_cmd(cmd, capture=False, check=True):
    return subprocess.run(cmd, text=True, capture_output=capture, check=check)

def write_remote_file(vm, path, content, append=False, sudo=False):
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    op = ">>" if append else ">"
    prefix = "sudo " if sudo else ""
    run_cmd(["multipass", "exec", vm, "--", "bash", "-c", f"echo '{b64}' | base64 -d | {prefix}tee {'-a ' if append else ''}{path} >/dev/null"])

def main():
    res = run_cmd(["multipass", "list", "--format=json"], capture=True)
    vm_info = [v for v in json.loads(res.stdout)["list"] if v["state"] == "Running"]
    vm_info.sort(key=lambda v: (0 if v["name"] == "manager" else 1, v["name"]))
    vms = [v["name"] for v in vm_info]
    vm_ips = {v["name"]: v["ipv4"][0] for v in vm_info}
    logging.info(f"Active VMs: {vm_ips}")

    # 1. /etc/hosts
    for name in vms:
        logging.info(f"Updating /etc/hosts on {name}...")
        lines = "\n".join(f"{ip} {other}" for other, ip in vm_ips.items() if other != name) + "\n"
        write_remote_file(name, "/etc/hosts", lines, append=True, sudo=True)

    # 2. SSH keys
    pub_keys = {}
    for vm in vms:
        logging.info(f"Generating SSH key on {vm}...")
        run_cmd(["multipass", "exec", vm, "--", "bash", "-c", "test -f ~/.ssh/id_rsa || ssh-keygen -q -t rsa -N '' -f ~/.ssh/id_rsa"])
        key = run_cmd(["multipass", "exec", vm, "--", "cat", "/home/ubuntu/.ssh/id_rsa.pub"], capture=True).stdout.strip()
        pub_keys[vm] = key

    for target in vms:
        logging.info(f"Adding authorized_keys on {target}...")
        other_keys = "\n".join(k for src, k in pub_keys.items() if src != target) + "\n"
        write_remote_file(target, "/home/ubuntu/.ssh/authorized_keys", other_keys, append=True, sudo=False)

    for src in vms:
        for dst in vms:
            if src != dst:
                run_cmd([
                    "multipass", "exec", src, "--", "ssh", "-n",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=5",
                    dst, "true"
                ])
    logging.info("SSH between all nodes verified!")

    # 3. NFS
    logging.info("Configuring NFS on manager...")
    run_cmd(["multipass", "exec", "manager", "--", "mkdir", "-p", "/home/ubuntu/cloud"])
    write_remote_file("manager", "/etc/exports", "/home/ubuntu/cloud *(rw,sync,no_root_squash,no_subtree_check)\n", append=False, sudo=True)
    run_cmd(["multipass", "exec", "manager", "--", "sudo", "systemctl", "enable", "--now", "rpcbind", "nfs-kernel-server"])
    run_cmd(["multipass", "exec", "manager", "--", "sudo", "exportfs", "-ra"])

    for worker in vms[1:]:
        logging.info(f"Mounting NFS on {worker}...")
        run_cmd(["multipass", "exec", worker, "--", "mkdir", "-p", "/home/ubuntu/cloud"])
        run_cmd(["multipass", "exec", worker, "--", "sudo", "systemctl", "enable", "--now", "rpcbind"])
        run_cmd(["multipass", "exec", worker, "--", "sudo", "bash", "-c", "sed -i '\\|/home/ubuntu/cloud|d' /etc/fstab"])
        write_remote_file(worker, "/etc/fstab", "manager:/home/ubuntu/cloud /home/ubuntu/cloud nfs defaults,_netdev 0 0\n", append=True, sudo=True)
        run_cmd(["multipass", "exec", worker, "--", "sudo", "bash", "-c", "mountpoint -q /home/ubuntu/cloud || mount -a"])

    # 4. mpi_hosts
    hosts_content = "\n".join(f"{vm} slots=4" for vm in vms) + "\n"
    write_remote_file("manager", "/home/ubuntu/cloud/mpi_hosts", hosts_content, append=False, sudo=False)
    logging.info("Cluster setup finished successfully!")

if __name__ == "__main__":
    main()