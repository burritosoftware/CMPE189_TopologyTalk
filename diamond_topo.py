from mininet.topo import Topo

class DiamondTopo(Topo):
    def build(self):
        h1 = self.addHost("h1")
        h2 = self.addHost("h2")

        s1 = self.addSwitch("s1")
        s2 = self.addSwitch("s2")
        s3 = self.addSwitch("s3")
        s4 = self.addSwitch("s4")

        self.addLink(h1, s1, bw=10, delay="5ms")
        self.addLink(s4, h2, bw=10, delay="5ms")

        # Slow path: s1 -> s2 -> s4
        self.addLink(s1, s2, bw=2, delay="50ms")
        self.addLink(s2, s4, bw=2, delay="50ms")

        # Better path: s1 -> s3 -> s4
        self.addLink(s1, s3, bw=10, delay="5ms")
        self.addLink(s3, s4, bw=10, delay="5ms")

topos = {
    "diamond": DiamondTopo
}
