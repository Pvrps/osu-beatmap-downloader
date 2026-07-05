{pkgs, ...}: {
  profile.python = {
    enable = true;
    pythonPackage = pkgs.python311;
    extraPackages = with pkgs; [
      unzip
      openvpn
      iproute2
      iptables
      procps
    ];
  };
}
