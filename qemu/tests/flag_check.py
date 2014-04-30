import re
import logging
import os.path
from virttest import utils_misc, data_dir
from autotest.client.shared import utils, error


@error.context_aware
def run(test, params, env):
    """
    flag_check test:
    steps:
    1. boot guest with -cpu model,+extra_flags (extra_flags is optional)
       a. no defined model_name in cfg file
          guest_model = host_model
       b. model_name defined in cfg file
          guest_model = params.get("cpu_model")
    2. get guest flags
    3. get expected model flags from dump file
       a. -cpu host: qemu_model = host_model
       b. guest_model > host_model --> expected_model = host_model
          e.g guest_model = Haswell, host_model = Sandybridge
              expected_model = Sandybridge
       c. guest_model < host_model --> expected_model = guest_model
    4. get extra flags
       a. add_flags = +flag
          1). flag is exposed to guest if it's supported in host
          2). flag is not supported to guest if it's unknown in host
          3). ignore "check", "enforce" which are params not flag
       b. del_flags = -flag
          flag is removed if it's supported in guest
       c. params check: check lack flag in host include unknow flag
    5. compare expected flag with flags in guest
       a. out_flags: not supported with some conf, this kinds of flag
          will be displayed in dump file, but not in guest.
          e.g tsc-dedline is not supported with -M rhel6.3.0
       b. option_flags: some flag is generated by kernel which is not
          defined in dump file. it's acceptable when display in guest.
          e.g rep_good
       expected_flags = expected_model_flags + add_flags - del_flags
                        - out_flags
       miss_flag = expected_flags - guest_flags
       unexpect_flag = guest_flags - expected_flags - option_flags

    :param test: Kvm test object
    :param params: Dictionary with the test parameters
    :param env: Dictionary with test environment.
    """

    def qemu_model_info(models_list, cpumodel):
        """
        Get cpumodel info from models_list
        :param models_list: all models info
        :param cpumodel: model name
        :return: model info of cpumodel
        """
        for model in models_list:
            if cpumodel in model:
                return model
        return None

    def qemu_support_flag(model_info, reg):
        """
        Get register's supported flags from model_info
        :param model_info: model_info get from dump file
        :param reg: reg name, e.g feature_edx
        """
        reg_re = re.compile(r".*%s.*\((.*)\)\n" % reg)
        flag = reg_re.search(model_info)
        try:
            if flag:
                return flag.groups()[0]
        except Exception, e:
            logging.error("Failed to get support flag %s" % e)

    def get_all_support_flags():
        """
        Get all supported flags with qemu query cmd.
        """
        qemu_binary = utils_misc.get_qemu_binary(params)
        cmd = qemu_binary + params.get("query_cmd", " -cpu ?")
        output = utils.system_output(cmd)
        flags_re = re.compile(params.get("pattern", "flags:(.*)"))
        flag_list = flags_re.search(output)
        flags = []
        if flag_list:
            for flag in flag_list.groups():
                flags += flag
        return set(map(utils_misc.Flag, flags))

    def get_extra_flag(extra_flags, symbol, lack_check=False):
        """
        Get added/removed flags
        :param extra_flags: exposed/removed flags. e.g "+sse4.1,+sse4.2"
        :param symbol: "+","-"
        :return: return all extra_flags if lack_check is true
                 return host supported flags if lack_check is false
        """
        flags = []
        re_flags = re.findall(symbol, extra_flags)
        for flag in re_flags:
            if lack_check:
                flags.append(flag)
            elif flag in host_flags:
                flags.append(flag)
        return set(map(utils_misc.Flag, flags))

    def get_guest_cpuflags(vm_session):
        """
        Get guest system cpuflags.

        :param vm_session: session to checked vm.
        :return: [corespond flags]
        """
        flags_re = re.compile(r'^flags\s*:(.*)$', re.MULTILINE)
        out = vm_session.cmd_output("cat /proc/cpuinfo")
        try:
            flags = flags_re.search(out).groups()[0].split()
            return set(map(utils_misc.Flag, flags))
        except Exception, e:
            logging.error("Failed to get guest cpu flags %s" % e)

    utils_misc.Flag.aliases = utils_misc.kvm_map_flags_aliases

    # Get all models' info from dump file
    dump_file = params.get("dump_file")
    default_dump_path = os.path.join(data_dir.get_deps_dir(), "cpuid")
    dump_path = params.get("dump_path", default_dump_path)
    cpuinfo_file = utils.unmap_url(dump_path, dump_file, dump_path)
    host_flags = utils_misc.get_cpu_flags()

    vm = env.get_vm(params["main_vm"])
    guest_cpumodel = vm.cpuinfo.model
    extra_flags = params.get("cpu_model_flags", " ")

    error.context("Boot guest with -cpu %s,%s" %
                  (guest_cpumodel, extra_flags), logging.info)
    vm.verify_alive()
    timeout = float(params.get("login_timeout", 240))
    session = vm.wait_for_login(timeout=timeout)

    # Get qemu model
    host_cpumodel = utils_misc.get_host_cpu_models()
    if guest_cpumodel not in host_cpumodel:
        qemu_model = host_cpumodel[0]
    else:
        qemu_model = guest_cpumodel
    error.context("Get model %s support flags" % qemu_model, logging.info)

    # Get flags for every reg from model's info
    models_info = utils.system_output("cat %s" % cpuinfo_file).split("x86")
    model_info = qemu_model_info(models_info, qemu_model)
    reg_list = params.get("reg_list", "feature_edx ").split()
    model_support_flags = " "
    if model_info:
        for reg in reg_list:
            reg_flags = qemu_support_flag(model_info, reg)
            if reg_flags:
                model_support_flags += " %s" % reg_flags
    model_support_flags = set(map(utils_misc.Flag,
                                  model_support_flags.split()))

    error.context("Get guest flags", logging.info)
    guest_flags = get_guest_cpuflags(session)

    error.context("Get expected flag list", logging.info)

    # out_flags is definded in dump file, but not in guest
    out_flags = params.get("out_flags", " ").split()
    out_flags = set(map(utils_misc.Flag, out_flags))
    # option_flags are generated by kernel or kvm, which are not definded in
    # dump file, but can be displayed in guest
    option_flags = params.get("option_flags", " ").split()
    if params['smp'] == '1' and 'up' not in option_flags:
        option_flags.append('up')
    option_flags = set(map(utils_misc.Flag, option_flags))
    # add_flags are exposed by +flag
    add_flags = get_extra_flag(extra_flags, "\+(\w+)")
    # del_flags are disabled by -flag
    del_flags = get_extra_flag(extra_flags, "\-(\w+)")
    expected_flags = ((model_support_flags | add_flags)
                      - del_flags - out_flags)
    # get all flags for host lack flag checking
    check_flags = get_extra_flag(extra_flags, "\+(\w+)", lack_check=True)
    host_flags = set(map(utils_misc.Flag, host_flags))
    lack_flags = set(expected_flags | check_flags) - host_flags

    if "check" in extra_flags:
        error.context("Check lack flag in host", logging.info)
        process_output = vm.process.get_output()
        miss_warn = []
        if lack_flags:
            for flag in lack_flags:
                if flag not in process_output:
                    miss_warn.extend(flag.split())
        if miss_warn:
            raise error.TestFail("no warning for lack flag %s" % miss_warn)

    error.context("Compare guest flags with expected flags", logging.info)
    all_support_flags = get_all_support_flags()
    missing_flags = expected_flags - guest_flags
    unexpect_flags = (guest_flags - expected_flags
                      - all_support_flags - option_flags)
    if missing_flags or unexpect_flags:
        raise error.TestFail("missing flags:\n %s\n"
                             "more flags than expected:\n %s\n"
                             "expected flags:\n %s\n"
                             "guest flags:\n %s\n"
                             % (missing_flags, unexpect_flags, expected_flags, guest_flags))
