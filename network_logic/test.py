from ryu.app import simple_switch_13  # Import the "brain"
from ryu.topology import event
from ryu.controller.handler import set_ev_cls

class MyNetworkOS(simple_switch_13.SimpleSwitch13):
    def __init__(self, *args, **kwargs):
        super(MyNetworkOS, self).__init__(*args, **kwargs)
        self.logger.info("Logic: L2 Learning Switch + Topology Discovery Active")

    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        self.logger.info(f"Switch {ev.switch.dp.id} is now online.")

