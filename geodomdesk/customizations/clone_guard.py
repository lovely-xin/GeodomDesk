"""Keep the official ID controls; reset a copied ID only when Ubuntu hardware changes."""


def apply(changes):
    change = changes.replace
    change(
        "libs/hbb_common/src/lib.rs",
        "pub mod compress;",
        'pub mod compress;\n#[cfg(target_os = "linux")]\nmod geodomdesk_clone_guard;',
    )
    change(
        "libs/hbb_common/src/config.rs",
        """    key_pair: KeyPair, // sk, pk""",
        """    key_pair: KeyPair, // sk, pk
    #[cfg(target_os = "linux")]
    #[serde(default, skip_serializing_if = "Vec::is_empty", deserialize_with = "deserialize_vec_string")]
    device_binding: Vec<String>,""",
    )
    change(
        "libs/hbb_common/src/config.rs",
        """        if store {
            config.store();
        }
        config
    }

    fn validate_or_decrypt_permanent_password_storage""",
        """        #[cfg(target_os = "linux")]
        {
            use crate::geodomdesk_clone_guard::{self, BindingUpdate};
            let update = geodomdesk_clone_guard::refresh(
                &mut config.id,
                &mut config.device_binding,
                geodomdesk_clone_guard::current_binding(),
            );
            store |= update != BindingUpdate::Unchanged;
            if update == BindingUpdate::Cloned {
                config.key_confirmed = false;
                config.keys_confirmed.clear();
                log::info!("Device hardware changed; replaced the copied client ID");
            }
        }
        if store {
            config.store();
        }
        config
    }

    fn validate_or_decrypt_permanent_password_storage""",
    )
